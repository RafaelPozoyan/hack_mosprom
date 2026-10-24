"""Карточка HR сохраняется вместе с обращением и выполняется из очереди PostgreSQL."""

import asyncio
import os
from contextlib import asynccontextmanager
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app import channels, scheduler
from app.bot import escalation
from app.db import Base
from app.models import Employee, Job, Message, Site, Staff, Ticket, TicketDeadline


@pytest_asyncio.fixture
async def db(monkeypatch):
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL нужен для проверки транзакций PostgreSQL")
    schema = "handoff_test_" + uuid4().hex
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
                        Ticket.__table__,
                        TicketDeadline.__table__,
                        Message.__table__,
                        Job.__table__,
                    ],
                )
            )

        @asynccontextmanager
        async def session():
            async with factory() as s:
                yield s

        monkeypatch.setattr(scheduler, "session", session)
        client = SimpleNamespace(send=AsyncMock(return_value="card-mid"))
        monkeypatch.setattr(channels, "max_client", lambda: client)
        monkeypatch.setattr(channels, "settings", lambda: SimpleNamespace(max_hr_chat_id=99))
        handoff = AsyncMock(
            return_value=escalation.Handoff(
                summary="Демо-проблема с доступом", suggested_action="Проверить доступ"
            )
        )
        monkeypatch.setattr(escalation, "_handoff", handoff)
        async with factory() as s:
            s.add(Site(code="GTP", name="Демо", kind="plant"))
            await s.flush()
            s.add(
                Employee(
                    id=1,
                    full_name="Демо Иван Петров",
                    first_name="Иван",
                    site_code="GTP",
                    start_date=date(2026, 10, 26),
                    probation_end=date(2027, 1, 26),
                    invite_code="demo",
                )
            )
            await s.commit()
        yield SimpleNamespace(session=factory, client=client, handoff=handoff)
    finally:
        await engine.dispose()
        async with admin.begin() as c:
            await c.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await admin.dispose()


async def create(db):
    async with db.session() as s:
        t = await escalation.create_ticket(s, await s.get(Employee, 1), "problem", "Демо: нужен доступ")
        return t.id


@pytest.mark.asyncio
async def test_ticket_and_pending_job_survive_session_close(db):
    tid = await create(db)
    db.client.send.assert_not_awaited()
    db.handoff.assert_not_awaited()
    async with db.session() as s:
        j = await s.scalar(select(Job).where(Job.kind == "handoff"))
        assert j.kind == "handoff" and j.ref == str(tid) and j.status == "pending"
        assert j.employee_id is None  # подготовка резюме не блокирует диалог сотрудника
    assert await scheduler.tick() == 1
    assert await scheduler.tick() == 0
    async with db.session() as s:
        t = await s.get(Ticket, tid)
        assert t.hr_chat_mid == "card-mid" and "Демо-проблема" in t.summary
        assert (await s.scalar(select(Job).where(Job.kind == "handoff"))).status == "done"
    db.client.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_job_insert_failure_rolls_back_ticket(db):
    async with db.session() as s:
        await s.execute(text("ALTER TABLE jobs ADD CONSTRAINT reject_handoff CHECK (kind <> 'handoff')"))
        await s.commit()
        with pytest.raises(IntegrityError):
            await escalation.create_ticket(s, await s.get(Employee, 1), "problem", "Демо")
        await s.rollback()
    async with db.session() as s:
        assert await s.scalar(select(func.count()).select_from(Ticket)) == 0
        assert await s.scalar(select(func.count()).select_from(Job)) == 0
    db.client.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_transport_failure_retries_card(db):
    tid = await create(db)
    db.client.send.side_effect = RuntimeError("transport failed")
    assert await scheduler.tick() == 1
    async with db.session() as s:
        j = await s.scalar(select(Job).where(Job.kind == "handoff"))
        assert j.status == "pending" and j.attempts == 1 and j.error
        assert (await s.get(Ticket, tid)).hr_chat_mid is None
        j.run_at = scheduler.utcnow()
        await s.commit()
    db.client.send.side_effect = None
    assert await scheduler.tick() == 1
    async with db.session() as s:
        assert (await s.scalar(select(Job).where(Job.kind == "handoff"))).status == "done"
        assert (await s.get(Ticket, tid)).hr_chat_mid == "card-mid"


@pytest.mark.asyncio
async def test_recruiter_retry_keeps_confirmed_group_card(db):
    async with db.session() as s:
        staff = Staff(name="Демо рекрутер", role="recruiter", max_user_id=7, invite_code="staff-demo")
        s.add(staff)
        await s.flush()
        emp = await s.get(Employee, 1)
        emp.recruiter_id = staff.id
        await s.commit()
    await create(db)

    async def send(msg, **kw):
        if "user_id" in kw:
            raise RuntimeError("recruiter unavailable")
        return "card-mid"

    db.client.send.side_effect = send
    assert await scheduler.tick() == 1
    async with db.session() as s:
        j = await s.scalar(select(Job).where(Job.kind == "handoff"))
        assert j.status == "pending"
        j.run_at = scheduler.utcnow()
        await s.commit()
    db.client.send.side_effect = None
    assert await scheduler.tick() == 1
    targets = [call.kwargs for call in db.client.send.await_args_list]
    assert targets == [{"chat_id": 99}, {"user_id": 7}, {"user_id": 7}]
    db.handoff.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("remove", [False, True])
async def test_closed_or_deleted_ticket_is_not_published(db, remove):
    tid = await create(db)
    async with db.session() as s:
        t = await s.get(Ticket, tid)
        if remove:
            await s.delete(t)
        else:
            t.status = "closed"
        await s.commit()
    assert await scheduler.tick() == 1
    db.client.send.assert_not_awaited()
    db.handoff.assert_not_awaited()


@pytest.mark.asyncio
async def test_closure_during_summary_generation_is_respected(db, monkeypatch):
    tid = await create(db)

    async def close_while_summarizing(*args):
        async with db.session() as s:
            t = await s.get(Ticket, tid)
            t.status = "closed"
            await s.commit()
        return escalation.Handoff(summary="Демо", suggested_action="Проверить")

    monkeypatch.setattr(escalation, "_handoff", close_while_summarizing)
    assert await scheduler.tick() == 1
    db.client.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_demo_without_max_keeps_summary_in_panel(db, monkeypatch):
    tid = await create(db)
    monkeypatch.setattr(channels, "max_client", lambda: None)
    assert await scheduler.tick() == 1
    async with db.session() as s:
        t = await s.get(Ticket, tid)
        assert "Демо-проблема" in t.summary and t.hr_chat_mid is None
        assert (await s.scalar(select(Job).where(Job.kind == "handoff"))).status == "done"


@pytest.mark.asyncio
async def test_sending_card_does_not_block_reply_message_insert(db):
    tid = await create(db)
    started, release = asyncio.Event(), asyncio.Event()

    async def send(*a, **kw):
        started.set()
        await release.wait()
        return "card-mid"

    db.client.send.side_effect = send
    worker = asyncio.create_task(scheduler.tick())
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        async with db.session() as s:
            s.add(Message(employee_id=1, ticket_id=tid, direction="out", author="hr", text="Демо-ответ"))
            await asyncio.wait_for(s.commit(), timeout=5)
    finally:
        release.set()
        await worker
