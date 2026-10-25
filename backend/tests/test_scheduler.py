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
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app import main, scheduler
from app.analytics import digest
from app.api import demo
from app.bot import scenario, surveys
from app.db import Base
from app.models import Employee, Job, Site, Staff, StepAttachment


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
            await c.run_sync(
                lambda conn: Base.metadata.create_all(
                    conn,
                    tables=[
                        Site.__table__,
                        Staff.__table__,
                        Employee.__table__,
                        Job.__table__,
                        StepAttachment.__table__,
                    ],
                )
            )

        @asynccontextmanager
        async def session():
            async with factory() as s:
                yield s

        monkeypatch.setattr(scheduler, "session", session)
        monkeypatch.setattr(scheduler, "_job_slots", asyncio.Semaphore(2))
        async with factory() as s:
            s.add(Site(code="GTP", name="Демо", kind="plant"))
            await s.flush()
            s.add(
                Employee(
                    id=1,
                    full_name="Демо Иван Петров",
                    first_name="Иван",
                    site_code="GTP",
                    status="active",
                    start_date=date(2026, 10, 26),
                    probation_end=date(2027, 1, 26),
                    invite_code="demo",
                )
            )
            await s.commit()
        yield SimpleNamespace(session=factory, schema=schema, url=url)
    finally:
        await engine.dispose()
        async with admin.begin() as c:
            await c.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await admin.dispose()


async def job(db, **kw):
    async with db.session() as s:
        j = Job(
            **(
                {
                    "employee_id": 1,
                    "kind": "step",
                    "ref": "welcome",
                    "run_at": scheduler.utcnow(),
                    "status": "pending",
                    "attempts": 0,
                }
                | kw
            )
        )
        s.add(j)
        await s.commit()
        return j.id


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["step", "reminder", "digest"])
async def test_dispatch_and_completed_job_is_not_repeated(db, monkeypatch, kind):
    handler = AsyncMock()
    module, name = {
        "step": (scenario, "run_step"),
        "reminder": (surveys, "remind"),
        "digest": (digest, "run"),
    }[kind]
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
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-c", script, db.url, db.schema, str(jid), stdout=asyncio.subprocess.PIPE
    )
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
            await asyncio.sleep(0.01)  # PostgreSQL должен заметить закрытие сокета убитого процесса

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
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=main.app), base_url="http://test"
        ) as api:
            assert not await scheduler.run_job(first)  # без перемотки будущий шаг не выполняется
            request = asyncio.create_task(api.post("/api/demo/employees/1/next"))
            try:
                await asyncio.wait_for(started.wait(), timeout=5)
                response = await api.post("/api/demo/employees/1/next")
                assert response.json()["status"] == "running"
                async with db.session() as s:
                    assert (await s.get(Job, first)).attempts == 1
                    assert (await s.get(Job, second)).status == "pending"
            finally:
                release.set()
                response = await asyncio.wait_for(request, timeout=5)
            assert response.json()["status"] == "done"
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
async def test_later_job_cannot_overtake_when_database_clock_is_behind(db, monkeypatch):
    async with db.session() as s:
        now = await s.scalar(select(func.now())) + timedelta(hours=1)
    monkeypatch.setattr(scheduler, "utcnow", lambda: now)
    earlier = await job(db, run_at=now - timedelta(minutes=2))
    later = await job(db, run_at=now - timedelta(minutes=1))
    handler = AsyncMock()
    monkeypatch.setattr(scenario, "run_step", handler)

    assert not await scheduler.run_job(later)
    handler.assert_not_awaited()
    assert await scheduler.run_job(earlier)
    assert await scheduler.run_job(later)
    assert handler.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("clock_offset", [-1, 1])
async def test_tick_uses_application_clock_for_due_candidates(db, monkeypatch, clock_offset):
    async with db.session() as s:
        now = await s.scalar(select(func.now())) + timedelta(hours=clock_offset)
    monkeypatch.setattr(scheduler, "utcnow", lambda: now)
    due = await job(db, run_at=now - timedelta(minutes=1))
    await job(db, run_at=now + timedelta(minutes=1))
    run = AsyncMock(return_value=True)
    monkeypatch.setattr(scheduler, "run_job", run)

    assert await scheduler.tick() == 1
    run.assert_awaited_once_with(due)


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


@pytest.mark.asyncio
async def test_manual_request_cannot_overlap_scheduler(db, monkeypatch):
    from fastapi import HTTPException

    from app.api import admin

    jid = await job(db)
    started, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def scheduled(*args):
        calls.append(args)
        started.set()
        await release.wait()

    monkeypatch.setattr(scenario, "run_step", scheduled)
    worker = asyncio.create_task(scheduler.run_job(jid))
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        async with db.session() as s:
            with pytest.raises(HTTPException) as exc:
                await admin.run_step_now(1, "welcome", None, s)
            assert exc.value.status_code == 409
        assert len(calls) == 1
    finally:
        release.set()
        await worker


@pytest_asyncio.fixture
async def manual_api(db, monkeypatch, tmp_path):
    from app.api import admin
    from app.config import settings

    async def get_session():
        async with db.session() as s:
            yield s

    monkeypatch.setattr(settings(), "media_dir", tmp_path)
    main.app.dependency_overrides[admin.get_session] = get_session
    main.app.dependency_overrides[admin.auth] = lambda: None
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=main.app), base_url="http://test"
        ) as api:
            yield api
    finally:
        main.app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_manual_owns_step_and_file_until_completion(db, manual_api, monkeypatch, tmp_path):
    from app import channels

    jid = await job(db, ref="d3_plan", run_at=scheduler.utcnow() + timedelta(days=1))
    started, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def handle(s, emp, ref):
        calls.append(ref)
        await s.commit()
        started.set()
        await release.wait()

    send = AsyncMock()
    monkeypatch.setattr(scenario, "run_step", handle)
    monkeypatch.setattr(channels, "send", send)
    url = "/api/employees/1/steps/d3_plan/run"
    worker = asyncio.create_task(manual_api.post(url, files={"file": ("../plan.pdf", b"first file")}))
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        assert not await scheduler.run_job(jid)
        assert (await manual_api.post(url, files={"file": ("second.pdf", b"second file")})).status_code == 409
        assert len(list(tmp_path.iterdir())) == 1  # отклонённый запрос не сохраняет файл
    finally:
        release.set()
        result = await worker
    assert result.status_code == 200
    assert calls == ["d3_plan"]
    send.assert_awaited_once()
    async with db.session() as s:
        assert (await s.get(Job, jid)).status == "done"
        attachment = await s.get(StepAttachment, jid)
        assert attachment.name == "plan.pdf"
        assert (tmp_path / attachment.file).read_bytes() == b"first file"
        assert send.await_args.args[2].file == attachment.file
    # Завершённый шаг можно намеренно отправить повторно с новым файлом.
    assert (await manual_api.post(url, files={"file": ("second.pdf", b"second file")})).status_code == 200
    assert calls == ["d3_plan", "d3_plan"]
    async with db.session() as s:
        assert len(list(await s.scalars(select(Job).where(Job.status == "done")))) == 2
        assert len(list(await s.scalars(select(StepAttachment)))) == 2


@pytest.mark.asyncio
async def test_manual_failure_retains_file_for_scheduler_retry(db, manual_api, monkeypatch, tmp_path):
    from app import channels

    monkeypatch.setattr(scenario, "run_step", AsyncMock())
    send = AsyncMock(side_effect=TimeoutError("test transport failure"))
    monkeypatch.setattr(channels, "send", send)
    url = "/api/employees/1/steps/d3_plan/run"
    assert (await manual_api.post(url, files={"file": ("plan.pdf", b"persisted")})).status_code == 503
    async with db.session() as s:
        j = await s.scalar(select(Job))
        assert j.status == "pending" and j.attempts == 1 and j.error
        attachment = await s.get(StepAttachment, j.id)
        assert (tmp_path / attachment.file).read_bytes() == b"persisted"
        jid = j.id
    assert (await manual_api.post(url, files={"file": ("replacement.pdf", b"wrong")})).status_code == 409
    assert len(list(tmp_path.iterdir())) == 1
    async with db.session() as s:
        (await s.get(Job, jid)).run_at = scheduler.utcnow()
        await s.commit()
    send.side_effect = None
    assert await scheduler.run_job(jid)
    assert send.await_args.args[2].file == attachment.file
    async with db.session() as s:
        assert (await s.get(Job, jid)).status == "done"


@pytest.mark.asyncio
async def test_manual_validates_employee_and_site_step_before_saving_file(db, manual_api, tmp_path):
    for url in [
        "/api/employees/999/steps/d3_plan/run",
        "/api/employees/1/steps/not-a-step/run",
        "/api/employees/1/steps/d1_end/run",
    ]:
        assert (await manual_api.post(url, files={"file": ("plan.pdf", b"unused")})).status_code == 404
    assert not list(tmp_path.iterdir())
    async with db.session() as s:
        assert not list(await s.scalars(select(Job)))


@pytest.mark.asyncio
async def test_interrupted_manual_request_keeps_recoverable_job_and_attachment(
    db, manual_api, monkeypatch, tmp_path
):
    from app import channels

    started = asyncio.Event()

    async def handle(*args):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(scenario, "run_step", handle)
    worker = asyncio.create_task(
        manual_api.post("/api/employees/1/steps/d3_plan/run", files={"file": ("plan.pdf", b"recover me")})
    )
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
    finally:
        worker.cancel()
        with pytest.raises(asyncio.CancelledError):
            await worker
    async with db.session() as s:
        j = await s.scalar(select(Job))
        assert j.status == "running"
        attachment = await s.get(StepAttachment, j.id)
        assert (tmp_path / attachment.file).read_bytes() == b"recover me"
        jid = j.id
    monkeypatch.setattr(scenario, "run_step", AsyncMock())
    send = AsyncMock()
    monkeypatch.setattr(channels, "send", send)
    assert await scheduler.run_job(jid)
    assert send.await_args.args[2].file == attachment.file
    async with db.session() as s:
        assert (await s.get(Job, jid)).status == "done"


@pytest.mark.asyncio
async def test_busy_executor_does_not_queue_a_late_manual_duplicate(db, manual_api, tmp_path):
    async with scheduler._job_slots, scheduler._job_slots:
        r = await asyncio.wait_for(
            manual_api.post("/api/employees/1/steps/d3_plan/run", files={"file": ("plan.pdf", b"unused")}),
            timeout=5,
        )
        assert r.status_code == 409
    assert not list(tmp_path.iterdir())
    async with db.session() as s:
        assert not list(await s.scalars(select(Job)))
