"""Владение задачами рассылки и восстановление проверяются на PostgreSQL."""

import asyncio
import os
import sys
from contextlib import asynccontextmanager
from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app import main, scheduler
from app.analytics import digest
from app.api import demo
from app.bot import scenario, surveys
from app.db import Base
from app.models import Employee, Job, Site, Staff


@pytest_asyncio.fixture
async def db(monkeypatch):
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL нужен для проверки блокировок PostgreSQL")
    schema = "scheduler_test_" + uuid4().hex
    admin = create_async_engine(url)
    async with admin.begin() as c:
        await c.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_async_engine(url, connect_args={"server_settings": {"search_path": schema}})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as c:
            await c.run_sync(lambda conn: Base.metadata.create_all(conn, tables=[
                Site.__table__, Staff.__table__, Employee.__table__, Job.__table__,
            ]))
        @asynccontextmanager
        async def session():
            async with factory() as s:
                yield s
        monkeypatch.setattr(scheduler, "session", session)
        monkeypatch.setattr(scheduler, "_job_slots", asyncio.Semaphore(2))
        async with factory() as s:
            s.add(Site(code="GTP", name="Демо", kind="plant"))
            await s.flush()
            s.add(Employee(id=1, full_name="Демо Иван Петров", first_name="Иван", site_code="GTP",
                           start_date=date(2026, 10, 26), probation_end=date(2027, 1, 26), invite_code="demo"))
            await s.commit()
        yield SimpleNamespace(session=factory, schema=schema, url=url)
    finally:
        await engine.dispose()
        async with admin.begin() as c:
            await c.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await admin.dispose()


async def job(db, **kw):
    async with db.session() as s:
        j = Job(**({"employee_id": 1, "kind": "step", "ref": "welcome", "run_at": scheduler.utcnow(),
                    "status": "pending", "attempts": 0} | kw))
        s.add(j)
        await s.commit()
        return j.id


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["step", "reminder", "digest"])
async def test_dispatch_and_completed_job_is_not_repeated(db, monkeypatch, kind):
    handler = AsyncMock()
    module, name = {"step": (scenario, "run_step"), "reminder": (surveys, "remind"), "digest": (digest, "run")}[kind]
    monkeypatch.setattr(module, name, handler)
    jid = await job(db, kind=kind, employee_id=None if kind == "digest" else 1)
    assert await scheduler.run_job(jid)
    assert not await scheduler.run_job(jid)
    handler.assert_awaited_once()
    async with db.session() as s:
        j = await s.get(Job, jid)
        assert j.status == "done" and j.attempts == 1 and j.done_at
        if kind == "digest":
            assert len(list(await s.scalars(select(Job).where(Job.status == "pending")))) == 1


@pytest.mark.asyncio
async def test_commit_does_not_release_job_or_employee_ownership(db, monkeypatch):
    first, second = await job(db), await job(db)
    started, release = asyncio.Event(), asyncio.Event()
    async def handle(s, *a):
        await s.commit()  # обработчики сценария сохраняют сообщения по ходу выполнения
        started.set()
        await release.wait()
    monkeypatch.setattr(scenario, "run_step", handle)
    worker = asyncio.create_task(scheduler.run_job(first))
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        assert not await scheduler.run_job(first)
        assert not await scheduler.run_job(second)
    finally:
        release.set()
        await worker
    assert await scheduler.run_job(second)


@pytest.mark.asyncio
async def test_retry_exhaustion_does_not_strand_other_jobs(db, monkeypatch):
    bad, good = await job(db, ref="bad"), await job(db, ref="good")
    async def handle(s, emp, ref):
        if ref == "bad":
            raise RuntimeError("test failure")
    monkeypatch.setattr(scenario, "run_step", handle)
    assert await scheduler.tick() == 2
    async with db.session() as s:
        assert (await s.get(Job, good)).status == "done"
    for _ in range(scheduler.MAX_ATTEMPTS - 1):
        async with db.session() as s:
            j = await s.get(Job, bad)
            assert j.status == "pending" and j.run_at > scheduler.utcnow()
            j.run_at = scheduler.utcnow()
            await s.commit()
        assert await scheduler.run_job(bad)
    async with db.session() as s:
        j = await s.get(Job, bad)
        assert j.status == "failed" and j.attempts == scheduler.MAX_ATTEMPTS


@pytest.mark.asyncio
async def test_digest_creation_is_serialized(db):
    async def ensure():
        async with db.session() as s:
            await scheduler.ensure_digest_job(s)
    await asyncio.gather(ensure(), ensure())
    async with db.session() as s:
        jobs = list(await s.scalars(select(Job)))
        assert len(jobs) == 1 and jobs[0].kind == "digest"


@pytest.mark.asyncio
async def test_killed_process_releases_running_job(db, monkeypatch):
    jid = await job(db)
    script = """
import asyncio, sys
from contextlib import asynccontextmanager
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from app import scheduler
from app.bot import scenario
engine = create_async_engine(sys.argv[1], connect_args={"server_settings": {"search_path": sys.argv[2]}})
factory = async_sessionmaker(engine, expire_on_commit=False)
@asynccontextmanager
async def session():
    async with factory() as s:
        yield s
async def handle(s, *a):
    await s.commit()
    print("running", flush=True)
    await asyncio.Event().wait()
scheduler.session, scenario.run_step = session, handle
asyncio.run(scheduler.run_job(int(sys.argv[3])))
"""
    proc = await asyncio.create_subprocess_exec(sys.executable, "-c", script, db.url, db.schema, str(jid),
                                               stdout=asyncio.subprocess.PIPE)
    try:
        assert await asyncio.wait_for(proc.stdout.readline(), timeout=10) == b"running\n"
        assert not await scheduler.run_job(jid)
        # Старт второго планировщика не сбрасывает состояние активного первого.
        checked = asyncio.Event()
        async def check_tick():
            async with db.session() as s:
                assert (await s.get(Job, jid)).status == "running"
            checked.set()
            await asyncio.Event().wait()
        monkeypatch.setattr(scheduler, "tick", check_tick)
        loop = asyncio.create_task(scheduler.loop())
        try:
            await asyncio.wait_for(checked.wait(), timeout=5)
        finally:
            loop.cancel()
            with pytest.raises(asyncio.CancelledError):
                await loop
    finally:
        if proc.returncode is None:
            proc.kill()
        await proc.wait()
    handler = AsyncMock()
    monkeypatch.setattr(scenario, "run_step", handler)
    async def recover():
        while not await scheduler.run_job(jid):
            await asyncio.sleep(.01)  # PostgreSQL должен заметить закрытие сокета убитого процесса
    await asyncio.wait_for(recover(), timeout=5)
    handler.assert_awaited_once()
    async with db.session() as s:
        j = await s.get(Job, jid)
        assert j.status == "done" and j.attempts == 2


@pytest.mark.asyncio
async def test_demo_next_is_durable_and_does_not_advance_while_running(db, monkeypatch):
    first = await job(db, run_at=scheduler.utcnow() + timedelta(days=1))
    second = await job(db, run_at=scheduler.utcnow() + timedelta(days=2))
    started, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
    async def handle(*a):
        started.set()
        await release.wait()
    async def run(jid):
        if await scheduler.run_job(jid):
            finished.set()
    async def get_session():
        async with db.session() as s:
            yield s
    monkeypatch.setattr(scenario, "run_step", handle)
    monkeypatch.setattr(demo, "run_job", run)
    main.app.dependency_overrides[demo.get_session] = get_session
    main.app.dependency_overrides[demo.auth] = lambda: None
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as api:
            assert not await scheduler.run_job(first)  # без перемотки будущий шаг не выполняется
            assert (await api.post("/api/demo/employees/1/next")).json()["ok"]
            await asyncio.wait_for(started.wait(), timeout=5)
            assert (await api.post("/api/demo/employees/1/next")).json()["ok"]
            async with db.session() as s:
                assert (await s.get(Job, first)).attempts == 1
                assert (await s.get(Job, second)).status == "pending"
    finally:
        release.set()
        await asyncio.wait_for(finished.wait(), timeout=5)
        main.app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_last_crashed_digest_is_failed_and_next_week_is_scheduled(db, monkeypatch):
    jid = await job(db, kind="digest", employee_id=None, status="running", attempts=scheduler.MAX_ATTEMPTS)
    handler = AsyncMock()
    monkeypatch.setattr(digest, "run", handler)
    assert await scheduler.run_job(jid)
    handler.assert_not_awaited()
    async with db.session() as s:
        assert (await s.get(Job, jid)).status == "failed"
        assert len(list(await s.scalars(select(Job).where(Job.status == "pending")))) == 1


@pytest.mark.asyncio
async def test_later_employee_job_cannot_overtake_earlier_due_job(db, monkeypatch):
    earlier, later = await job(db), await job(db)
    monkeypatch.setattr(scenario, "run_step", AsyncMock())
    assert not await scheduler.run_job(later)
    assert await scheduler.run_job(earlier)
    assert await scheduler.run_job(later)


@pytest.mark.asyncio
async def test_persisted_demo_request_survives_loss_of_background_task(db, monkeypatch):
    jid = await job(db, status="running", run_at=scheduler.utcnow() + timedelta(days=1))
    handler = AsyncMock()
    monkeypatch.setattr(scenario, "run_step", handler)
    assert await scheduler.tick() == 1
    handler.assert_awaited_once()
    async with db.session() as s:
        j = await s.get(Job, jid)
        assert j.status == "done" and j.attempts == 1
