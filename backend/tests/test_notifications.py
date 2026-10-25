"""Уведомления MAX переживают rollback, повтор и отмену процесса; mid подтверждается после сети."""

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
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.channels import OutMessage, notifications
from app.db import Base
from app.models import Employee, MaxNotification, Message, Site, Staff, Ticket, TicketDeadline


@pytest_asyncio.fixture
async def db(monkeypatch):
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL нужен для проверки очереди PostgreSQL")
    schema = "notification_test_" + uuid4().hex
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
                        MaxNotification.__table__,
                    ],
                )
            )

        @asynccontextmanager
        async def session():
            async with factory() as s:
                yield s

        client = SimpleNamespace(send=AsyncMock(return_value="confirmed-mid"))
        monkeypatch.setattr(notifications, "session", session)
        monkeypatch.setattr(notifications, "max_client", lambda: client)
        async with factory() as s:
            s.add(Site(code="GTP", name="Демо", kind="plant"))
            await s.flush()
            s.add(
                Employee(
                    id=1,
                    full_name="Демо Иван",
                    first_name="Иван",
                    site_code="GTP",
                    start_date=date(2026, 10, 26),
                    probation_end=date(2027, 1, 26),
                    invite_code="demo",
                )
            )
            await s.flush()
            s.add(Ticket(id=1, employee_id=1, kind="problem", subject="Демо"))
            await s.commit()
        yield SimpleNamespace(session=factory, client=client)
    finally:
        await engine.dispose()
        async with admin.begin() as c:
            await c.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await admin.dispose()


async def queue(db, **kwargs):
    async with db.session() as s:
        ident = await notifications.enqueue(s, OutMessage(text="Демо"), **kwargs)
        await s.commit()
        return ident


async def ready(db, ident):
    async with db.session() as s:
        row = await s.get(MaxNotification, ident)
        row.available_at = func.clock_timestamp()
        await s.commit()


@pytest.mark.asyncio
async def test_rollback_and_event_dedup_preserve_payload(db):
    async with db.session() as s:
        (await s.get(Employee, 1)).first_name = "Изменено"
        await notifications.enqueue(s, OutMessage(text="Не отправлять"), user_id=1)
        await s.rollback()
        assert await s.scalar(select(func.count()).select_from(MaxNotification)) == 0
        assert (await s.get(Employee, 1)).first_name == "Иван"
    ident = await queue(db, user_id=1, dedup_key="event-1")
    async with db.session() as s:
        assert (
            await notifications.enqueue(s, OutMessage(text="Другой текст"), user_id=1, dedup_key="event-1")
            == ident
        )
        await s.commit()
    assert await queue(db, chat_id=1, dedup_key="event-1") != ident
    db.client.send.assert_not_awaited()
    assert await notifications.process_one()
    assert await notifications.process_one()
    assert not await notifications.process_one()
    assert [c.args[0].text for c in db.client.send.await_args_list] == ["Демо", "Демо"]
    async with db.session() as s:
        row = await s.get(MaxNotification, ident)
        assert row.status == "done" and row.max_mid == "confirmed-mid"


@pytest.mark.asyncio
@pytest.mark.parametrize("result", [RuntimeError("private details"), None, ""])
async def test_retry_limit_and_order_do_not_block_other_recipients(db, result):
    first = await queue(db, user_id=1)
    second = await queue(db, user_id=1)
    await queue(db, user_id=2)
    db.client.send.side_effect = result if isinstance(result, Exception) else None
    db.client.send.return_value = result
    assert await notifications.process_one()
    assert await notifications.process_one()  # другой получатель не ждёт первого
    assert not await notifications.process_one()  # второй того же пользователя ещё ждёт
    for _ in range(2):
        await ready(db, first)
        assert await notifications.process_one()
    async with db.session() as s:
        row = await s.get(MaxNotification, first)
        assert row.status == "failed" and row.attempts == 3
        assert row.error in {"RuntimeError", "ValueError"}
        assert (await s.get(MaxNotification, second)).attempts == 0
    db.client.send.side_effect, db.client.send.return_value = None, "confirmed-mid"
    assert await notifications.process_one()  # терминальная ошибка не останавливает HR навсегда


@pytest.mark.asyncio
@pytest.mark.parametrize("interruptions", [1, 3])
async def test_cancelled_worker_keeps_attempt_and_resumes_without_token_loss(db, monkeypatch, interruptions):
    ident = await queue(db, user_id=1)
    monkeypatch.setattr(notifications, "max_client", lambda: None)
    assert not await notifications.process_one()
    monkeypatch.setattr(notifications, "max_client", lambda: db.client)
    db.client.send.side_effect = asyncio.CancelledError()
    for attempt in range(1, interruptions + 1):
        with pytest.raises(asyncio.CancelledError):
            await notifications.process_one()
        async with db.session() as s:
            row = await s.get(MaxNotification, ident)
            assert row.status == "running" and row.attempts == attempt
        await ready(db, ident)
    db.client.send.side_effect = None
    assert await notifications.process_one()
    async with db.session() as s:
        row = await s.get(MaxNotification, ident)
        assert row.status == ("done" if interruptions == 1 else "failed")
        assert row.attempts == (2 if interruptions == 1 else 3)


@pytest.mark.asyncio
async def test_card_confirmation_never_locks_ticket_during_network(db):
    await queue(db, chat_id=99, ticket_id=1, kind="hr_card")

    async def send(*args, **kwargs):
        async with db.session() as s:
            ticket = await s.scalar(select(Ticket).where(Ticket.id == 1).with_for_update(of=Ticket))
            assert ticket.hr_chat_mid is None
            ticket.status = "closed"
            await asyncio.wait_for(s.commit(), timeout=2)
        return "real-card-mid"

    db.client.send.side_effect = send
    assert await notifications.process_one()
    async with db.session() as s:
        assert (await s.get(Ticket, 1)).hr_chat_mid == "real-card-mid"


@pytest.mark.asyncio
async def test_closed_card_cancelled_but_service_response_delivered(db):
    ident = await queue(db, chat_id=99, ticket_id=1, kind="hr_card")
    await queue(db, chat_id=99, ticket_id=1)
    async with db.session() as s:
        (await s.get(Ticket, 1)).status = "closed"
        await s.commit()
    assert await notifications.process_one()
    db.client.send.assert_not_awaited()
    assert await notifications.process_one()
    db.client.send.assert_awaited_once()
    async with db.session() as s:
        assert (await s.get(MaxNotification, ident)).status == "cancelled"


@pytest.mark.asyncio
async def test_parallel_workers_do_not_send_one_notification_twice(db):
    await queue(db, user_id=1)
    entered, release = asyncio.Event(), asyncio.Event()

    async def send(*args, **kwargs):
        entered.set()
        await release.wait()
        return "confirmed-mid"

    db.client.send.side_effect = send
    worker = asyncio.create_task(notifications.process_one())
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        assert not await notifications.process_one()
    finally:
        release.set()
        await worker
    db.client.send.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("model", [Ticket, Employee])
async def test_delete_during_send_removes_notification_without_retry(db, model):
    ident = await queue(db, chat_id=99, ticket_id=1, kind="hr_card")

    async def send(*args, **kwargs):
        async with db.session() as s:
            await s.delete(await s.get(model, 1))
            await asyncio.wait_for(s.commit(), timeout=2)
        return "confirmed-mid"

    db.client.send.side_effect = send
    assert await notifications.process_one()
    async with db.session() as s:
        assert await s.get(MaxNotification, ident) is None
    assert not await notifications.process_one()


@pytest.mark.asyncio
async def test_opposite_recipient_orders_do_not_deadlock(db):
    entered = asyncio.Event()

    async def other():
        async with db.session() as s:
            entered.set()
            await notifications.enqueue(s, OutMessage(text="B"), user_id=2)
            await notifications.enqueue(s, OutMessage(text="B"), user_id=1)
            await s.commit()

    task = None
    try:
        async with db.session() as s:
            await notifications.enqueue(s, OutMessage(text="A"), user_id=1)
            task = asyncio.create_task(other())
            await asyncio.wait_for(entered.wait(), timeout=2)
            await asyncio.wait_for(notifications.enqueue(s, OutMessage(text="A"), user_id=2), timeout=2)
            await s.commit()
        await asyncio.wait_for(task, timeout=2)
    finally:
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    async with db.session() as s:
        rows = list(await s.scalars(select(MaxNotification).order_by(MaxNotification.id)))
        assert [(r.payload["text"], r.user_id) for r in rows] == [("A", 1), ("A", 2), ("B", 2), ("B", 1)]
