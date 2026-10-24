"""Доставка, порядок и восстановление исходящих сообщений проверяются на PostgreSQL."""

import asyncio
import os
import sys
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

from app import channels
from app.channels import Button, OutMessage, outbox
from app.db import Base
from app.models import Employee, MaxDelivery, Message, Site, Staff, Ticket


@pytest_asyncio.fixture
async def db(monkeypatch):
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL нужен для проверки очереди PostgreSQL")
    schema = "outbox_test_" + uuid4().hex
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
                        Message.__table__,
                        MaxDelivery.__table__,
                    ],
                )
            )

        @asynccontextmanager
        async def session():
            async with factory() as s:
                yield s

        monkeypatch.setattr(outbox, "session", session)
        client = SimpleNamespace(send=AsyncMock(return_value="mid-1"), edit=AsyncMock())
        monkeypatch.setattr(outbox, "max_client", lambda: client)
        monkeypatch.setattr(channels, "max_client", lambda: client)
        async with factory() as s:
            s.add(Site(code="GTP", name="Демо", kind="plant"))
            await s.flush()
            for i in (1, 2):
                s.add(
                    Employee(
                        id=i,
                        full_name="Демо Иван",
                        first_name="Иван",
                        site_code="GTP",
                        start_date=date(2026, 10, 26),
                        probation_end=date(2027, 1, 26),
                        invite_code=str(i),
                        channel="max",
                        max_user_id=i,
                    )
                )
            await s.commit()
        yield SimpleNamespace(session=factory, client=client, url=url, schema=schema)
    finally:
        await engine.dispose()
        async with admin.begin() as c:
            await c.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await admin.dispose()


async def enqueue(db, emp_id=1, msg=None):
    async with db.session() as s:
        return (await channels.send(s, await s.get(Employee, emp_id), msg or OutMessage(text="Демо"))).id


@pytest.mark.asyncio
async def test_commit_before_delivery_and_confirmed_send_not_repeated(db):
    msg = OutMessage(
        text="Демо", buttons=[[Button("Да", payload="a:yes")]], image="demo.png", file="demo.pdf"
    )
    mid = await enqueue(db, msg=msg)
    db.client.send.assert_not_awaited()
    assert await outbox.process_one()  # новая сессия, исходная уже закрыта
    assert not await outbox.process_one()
    db.client.send.assert_awaited_once_with(msg, user_id=1)
    async with db.session() as s:
        assert (await s.get(Message, mid)).max_mid == "mid-1"
        assert (await s.get(MaxDelivery, mid)).status == "done"


@pytest.mark.asyncio
async def test_failed_enqueue_rolls_back_message_and_state(db):
    async with db.session() as s:
        await s.execute(text("ALTER TABLE max_deliveries ADD CONSTRAINT reject_delivery CHECK (user_id < 0)"))
        emp = await s.get(Employee, 1)
        emp.state = {"test": "not committed"}
        with pytest.raises(IntegrityError):
            await channels.send(s, emp, OutMessage(text="Не отправлять"))
        await s.rollback()
    async with db.session() as s:
        assert await s.scalar(select(func.count()).select_from(Message)) == 0
        assert (await s.get(Employee, 1)).state == {}
    db.client.send.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("result", [TimeoutError("possibly accepted"), None])
async def test_uncertain_send_retries_then_fails_without_overtaking(db, result):
    first, later = await enqueue(db), await enqueue(db)
    db.client.send.side_effect = result if isinstance(result, Exception) else None
    db.client.send.return_value = result
    for attempt in range(1, outbox.MAX_ATTEMPTS + 1):
        assert await outbox.process_one()
        assert not await outbox.process_one()
        async with db.session() as s:
            row = await s.get(MaxDelivery, first)
            assert (
                row.attempts == attempt
                and row.error
                and row.available_at > await s.scalar(select(func.now()))
            )
            assert row.status == ("failed" if attempt == outbox.MAX_ATTEMPTS else "pending")
            assert (await s.get(MaxDelivery, later)).attempts == 0
            row.available_at = func.now()
            await s.commit()
    db.client.send.side_effect, db.client.send.return_value = None, "other-mid"
    await enqueue(db, emp_id=2)
    assert await outbox.process_one()  # другой получатель не заблокирован ошибкой первого
    assert not await outbox.process_one()


@pytest.mark.asyncio
async def test_concurrent_workers_preserve_recipient_order(db):
    _first, later = await enqueue(db), await enqueue(db)
    await enqueue(db, emp_id=2)
    started, release = asyncio.Event(), asyncio.Event()

    async def send(*args, **kw):
        if kw["user_id"] == 2:
            return "other-mid"
        started.set()
        await release.wait()
        return "mid"

    db.client.send.side_effect = send
    worker = asyncio.create_task(outbox.process_one())
    try:
        await asyncio.wait_for(started.wait(), 5)
        assert await outbox.process_one()  # медленная отправка не задерживает другой чат
        assert not await outbox.process_one()
        async with db.session() as s:
            assert (await s.get(MaxDelivery, later)).attempts == 0
    finally:
        release.set()
        await worker
    assert await outbox.process_one()
    assert db.client.send.await_count == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("delivered", [False, True])
async def test_edit_before_or_after_send_and_legacy_message(db, delivered):
    mid = await enqueue(db)
    if delivered:
        await outbox.process_one()
        async with db.session() as s:
            await s.delete(await s.get(MaxDelivery, mid))  # старое сообщение до появления очереди
            await s.commit()
    async with db.session() as s:
        await channels.edit(
            s, await s.get(Employee, 1), await s.get(Message, mid), OutMessage(text="Изменено")
        )
    assert await outbox.process_one()
    if delivered:
        db.client.edit.assert_awaited_once_with("mid-1", OutMessage(text="Изменено"))
        assert db.client.send.await_count == 1
    else:
        db.client.send.assert_awaited_once_with(OutMessage(text="Изменено"), user_id=1)
        db.client.edit.assert_not_awaited()


@pytest.mark.asyncio
async def test_edit_waits_for_inflight_send_and_is_not_lost(db):
    mid = await enqueue(db)
    started, release, editing = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def send(*args, **kw):
        started.set()
        await release.wait()
        return "mid"

    async def edit():
        async with db.session() as s:
            emp, m = await s.get(Employee, 1), await s.get(Message, mid)
            editing.set()
            await channels.edit(s, emp, m, OutMessage(text="После отправки"))

    db.client.send.side_effect = send
    worker = asyncio.create_task(outbox.process_one())
    task = None
    try:
        await asyncio.wait_for(started.wait(), 5)
        task = asyncio.create_task(edit())
        await asyncio.wait_for(editing.wait(), 5)
        assert not task.done()
    finally:
        release.set()
        await worker
        if task:
            await task
    assert await outbox.process_one()
    db.client.edit.assert_awaited_once_with("mid", OutMessage(text="После отправки"))
    assert db.client.send.await_count == 1


@pytest.mark.asyncio
async def test_missing_token_preserves_queue_and_demo_stays_local(db, monkeypatch):
    mid = await enqueue(db)
    monkeypatch.setattr(outbox, "max_client", lambda: None)
    assert not await outbox.process_one()
    async with db.session() as s:
        assert (await s.get(MaxDelivery, mid)).attempts == 0
        emp = await s.get(Employee, 2)
        emp.channel = "web"
        m = await channels.send(s, emp, OutMessage(text="Демо"))
        await channels.edit(s, emp, m, OutMessage(text="Демо изменено"))
        assert await s.get(MaxDelivery, m.id) is None
        assert m.text == "Демо изменено"
    db.client.send.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("last_attempt", [False, True])
async def test_killed_sender_is_retried_even_after_possible_acceptance(db, last_attempt):
    mid = await enqueue(db)
    if last_attempt:
        async with db.session() as s:
            (await s.get(MaxDelivery, mid)).attempts = outbox.MAX_ATTEMPTS - 1
            await s.commit()
    script = """
import asyncio, sys
from contextlib import asynccontextmanager
from types import SimpleNamespace
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from app.channels import outbox
engine = create_async_engine(sys.argv[1], connect_args={'server_settings': {'search_path': sys.argv[2]}})
factory = async_sessionmaker(engine, expire_on_commit=False)
@asynccontextmanager
async def session():
    async with factory() as s: yield s
async def send(*a, **kw):
    print('POSSIBLY_ACCEPTED', flush=True)
    await asyncio.Event().wait()
outbox.session = session
outbox.max_client = lambda: SimpleNamespace(send=send)
asyncio.run(outbox.process_one())
"""
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-c", script, db.url, db.schema, stdout=asyncio.subprocess.PIPE
    )
    try:
        assert await asyncio.wait_for(proc.stdout.readline(), 10) == b"POSSIBLY_ACCEPTED\n"
        assert not await outbox.process_one()
    finally:
        if proc.returncode is None:
            proc.kill()
        await proc.wait()
    async with db.session() as s:
        row = await s.get(MaxDelivery, mid)
        assert row.status == "running"
        assert row.attempts == (outbox.MAX_ATTEMPTS if last_attempt else 1)
        assert not await outbox.process_one()  # после аварии также выдерживаем паузу
        row.available_at = func.now()
        await s.commit()
    assert await outbox.process_one()
    async with db.session() as s:
        row = await s.get(MaxDelivery, mid)
        if last_attempt:
            db.client.send.assert_not_awaited()
            assert row.status == "failed" and row.error
        else:
            db.client.send.assert_awaited_once()
            assert row.status == "done" and row.attempts == 2
            assert (await s.get(Message, mid)).max_mid == "mid-1"
