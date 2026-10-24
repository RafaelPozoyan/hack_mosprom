"""Сбой входящего события не оставляет частично сохранённый ответ или исходящее сообщение."""

import asyncio
import os
from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app import channels
from app.bot import inbox, surveys, updates
from app.db import Base
from app.models import Employee, Job, MaxDelivery, MaxUpdate, Message, Site, Staff, SurveyAnswer, Ticket


@pytest_asyncio.fixture
async def db(monkeypatch):
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL нужен для проверки атомарности PostgreSQL")
    schema = "atomic_test_" + uuid4().hex
    owner = create_async_engine(url)
    async with owner.begin() as c:
        await c.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_async_engine(url, connect_args={"server_settings": {"search_path": schema}})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as c:
            await c.run_sync(
                lambda conn: Base.metadata.create_all(
                    conn,
                    tables=[
                        model.__table__
                        for model in (
                            Site,
                            Staff,
                            Employee,
                            Job,
                            Ticket,
                            Message,
                            SurveyAnswer,
                            MaxDelivery,
                            MaxUpdate,
                        )
                    ],
                )
            )

        @asynccontextmanager
        async def session():
            async with factory() as s:
                yield s

        monkeypatch.setattr(inbox, "session", session)
        monkeypatch.setattr(updates, "session", session)
        monkeypatch.setattr(updates, "_locks", defaultdict(asyncio.Lock))
        client = SimpleNamespace(answer_callback=AsyncMock(), send=AsyncMock())
        monkeypatch.setattr(updates, "max_client", lambda: client)
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
                    channel="max",
                    max_user_id=1,
                    status="active",
                    state={"survey": {"id": "day3", "q": 0, "key": "run", "selected": []}},
                )
            )
            await s.commit()
        yield SimpleNamespace(session=factory, client=client)
    finally:
        await engine.dispose()
        async with owner.begin() as c:
            await c.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await owner.dispose()


def callback(payload="s:day3:docs:run:a", mid=None):
    return {
        "update_type": "message_callback",
        "callback": {"user": {"user_id": 1}, "callback_id": "click-1", "payload": payload},
        "message": {"body": {"mid": mid}},
    }


async def snapshot(db):
    async with db.session() as s:
        emp = await s.get(Employee, 1)
        counts = [
            await s.scalar(select(func.count()).select_from(model))
            for model in (SurveyAnswer, Message, MaxDelivery)
        ]
        return emp.state, counts


async def retry_now(db, error="RuntimeError"):
    async with db.session() as s:
        row = await s.scalar(select(MaxUpdate))
        assert row.status == "pending" and row.error == error
        row.available_at = inbox.utcnow()
        await s.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_at", ["next_question", "after_handler"])
async def test_failed_answer_rolls_back_and_retry_applies_once(db, monkeypatch, failure_at):
    original = await snapshot(db)
    queue = channels.hub.subscribe(1)
    target, name = (surveys, "_ask") if failure_at == "next_question" else (inbox, "process_max_update")
    handle = getattr(target, name)

    async def fail(*args, **kwargs):
        if failure_at == "after_handler":
            await handle(*args, **kwargs)
        raise RuntimeError("interrupted after saving answer")

    monkeypatch.setattr(target, name, fail)
    try:
        await inbox.enqueue(callback())
        assert await inbox.process_one()
        assert await snapshot(db) == original
        assert queue.empty()
        monkeypatch.setattr(target, name, handle)
        await retry_now(db)
        assert await inbox.process_one()
        state, counts = await snapshot(db)
        assert state["survey"]["q"] == 1 and counts == [1, 1, 1]
        assert queue.qsize() == 1
        await inbox.enqueue(callback())
        assert not await inbox.process_one()
        assert await snapshot(db) == (state, counts)
        db.client.send.assert_not_awaited()
    finally:
        channels.hub.unsubscribe(1, queue)


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_uncommitted_result_is_invisible_and_cancellation_recovers(db, monkeypatch, cancel):
    original = await snapshot(db)
    started, release = asyncio.Event(), asyncio.Event()
    handle = inbox.process_max_update

    async def pause(*args, **kwargs):
        await handle(*args, **kwargs)
        started.set()
        await release.wait()

    monkeypatch.setattr(inbox, "process_max_update", pause)
    queue = channels.hub.subscribe(1)
    await inbox.enqueue(callback())
    worker = asyncio.create_task(inbox.process_one())
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        assert await snapshot(db) == original
        assert queue.empty()
        if cancel:
            worker.cancel()
            with pytest.raises(asyncio.CancelledError):
                await worker
            assert await snapshot(db) == original and queue.empty()
            monkeypatch.setattr(inbox, "process_max_update", handle)
            assert await inbox.process_one()
        else:
            release.set()
            assert await worker
        state, counts = await snapshot(db)
        assert state["survey"]["q"] == 1 and counts == [1, 1, 1]
        assert queue.qsize() == 1
        async with db.session() as s:
            row = await s.scalar(select(MaxUpdate))
            assert row.status == "done" and row.payload == {} and row.done_at
    finally:
        release.set()
        await asyncio.gather(worker, return_exceptions=True)
        channels.hub.unsubscribe(1, queue)


@pytest.mark.asyncio
async def test_retry_does_not_toggle_committed_selection_twice(db, monkeypatch):
    async with db.session() as s:
        emp = await s.get(Employee, 1)
        emp.state = {"survey": {"id": "month2", "q": 1, "key": "run", "selected": []}}
        msg = await channels.send(
            s, emp, surveys.question_message(emp, "month2", surveys.questions(emp, "month2")[1])
        )
        msg.max_mid = "question-1"
        (await s.get(MaxDelivery, msg.id)).status = "done"
        await s.commit()
    original = await snapshot(db)
    handle = inbox.process_max_update

    async def fail(*args, **kwargs):
        await handle(*args, **kwargs)
        raise RuntimeError("interrupted after toggle")

    monkeypatch.setattr(inbox, "process_max_update", fail)
    event = callback("m:month2:helpers:run:peers", mid="question-1")
    await inbox.enqueue(event)
    assert await inbox.process_one()
    assert await snapshot(db) == original
    async with db.session() as s:
        assert (await s.get(MaxDelivery, msg.id)).status == "done"
    monkeypatch.setattr(inbox, "process_max_update", handle)
    await retry_now(db)
    assert await inbox.process_one()
    async with db.session() as s:
        assert (await s.get(Employee, 1)).state["survey"]["selected"] == ["peers"]
        assert (await s.get(MaxDelivery, msg.id)).status == "pending"
    await inbox.enqueue(event)
    assert not await inbox.process_one()


@pytest.mark.asyncio
async def test_sql_error_rolls_back_answer_but_saves_retry(db):
    original = await snapshot(db)
    async with db.session() as s:
        await s.execute(text("ALTER TABLE max_deliveries ADD CONSTRAINT reject_delivery CHECK (user_id < 0)"))
        await s.commit()
    await inbox.enqueue(callback())
    assert await inbox.process_one()
    assert await snapshot(db) == original
    await retry_now(db, "IntegrityError")
    async with db.session() as s:
        await s.execute(text("ALTER TABLE max_deliveries DROP CONSTRAINT reject_delivery"))
        await s.commit()
    assert await inbox.process_one()
    state, counts = await snapshot(db)
    assert state["survey"]["q"] == 1 and counts == [1, 1, 1]
