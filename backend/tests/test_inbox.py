"""Регрессии входящей очереди: настоящий PostgreSQL, без сетевого подключения к MAX."""

import asyncio
import os
import sys
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app import main
from app.bot import inbox, updates
from app.models import MaxUpdate


def event(mid="m1", user=1):
    return {
        "update_type": "message_created",
        "timestamp": 1,
        "message": {
            "sender": {"user_id": user},
            "recipient": {"chat_id": user},
            "body": {"mid": mid, "text": "Демо-вопрос"},
        },
    }


@pytest_asyncio.fixture
async def db(monkeypatch):
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL нужен для проверки блокировок PostgreSQL")
    schema = "inbox_test_" + uuid4().hex
    admin = create_async_engine(url)
    async with admin.begin() as c:
        await c.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_async_engine(url, connect_args={"server_settings": {"search_path": schema}})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as c:
            await c.run_sync(MaxUpdate.__table__.create)

        @asynccontextmanager
        async def session():
            async with factory() as s:
                yield s

        monkeypatch.setattr(inbox, "session", session)
        yield factory
    finally:
        await engine.dispose()
        async with admin.begin() as c:
            await c.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await admin.dispose()


@pytest_asyncio.fixture
async def api(monkeypatch):
    monkeypatch.setattr(
        main, "settings", lambda: SimpleNamespace(max_token="test", max_webhook_secret="secret")
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=main.app, raise_app_exceptions=False),
        base_url="http://test",
        headers={"X-Max-Bot-Api-Secret": "secret"},
    ) as client:
        yield client


@pytest.mark.asyncio
async def test_webhook_commits_before_ack_and_deduplicates(db, api, monkeypatch):
    handler = AsyncMock()
    monkeypatch.setattr(inbox, "process_max_update", handler)
    results = await asyncio.gather(*(api.post("/max/webhook", json=event()) for _ in range(6)))
    assert all(r.status_code == 200 for r in results)
    async with db() as s:
        assert await s.scalar(select(func.count()).select_from(MaxUpdate)) == 1
    handler.assert_not_awaited()
    assert await inbox.process_one()
    assert not await inbox.process_one()
    assert (await api.post("/max/webhook", json=event())).status_code == 200
    assert not await inbox.process_one()
    handler.assert_awaited_once_with(event(), raise_errors=True, context=ANY)
    async with db() as s:
        row = await s.scalar(select(MaxUpdate))
        assert row.status == "done" and row.payload == {} and row.done_at


@pytest.mark.asyncio
async def test_webhook_failure_and_auth(api, monkeypatch):
    enqueue = AsyncMock(side_effect=RuntimeError("database unavailable"))
    monkeypatch.setattr(inbox, "enqueue", enqueue)
    assert (
        await api.post("/max/webhook", json=event(), headers={"X-Max-Bot-Api-Secret": "wrong"})
    ).status_code == 403
    enqueue.assert_not_awaited()
    assert (await api.post("/max/webhook", json=event())).status_code == 500
    monkeypatch.setattr(main, "settings", lambda: SimpleNamespace(max_token="", max_webhook_secret=""))
    assert (await api.post("/max/webhook", json=event())).status_code == 503


@pytest.mark.asyncio
async def test_bad_or_unsupported_events(api):
    for body in [[], {}, {"update_type": "message_callback", "callback": None}]:
        assert (await api.post("/max/webhook", json=body)).status_code == 422
    assert (await api.post("/max/webhook", content=b"{")).status_code == 422
    assert (await api.post("/max/webhook", json={"update_type": "bot_stopped"})).status_code == 200


@pytest.mark.asyncio
async def test_parallel_workers_keep_user_order_but_allow_other_users(db, monkeypatch):
    for upd in [event("first"), event("second"), event("other", 2)]:
        await inbox.enqueue(upd)
    started, release = asyncio.Event(), asyncio.Event()
    seen = []

    async def handle(upd, **kw):
        mid = upd["message"]["body"]["mid"]
        seen.append(mid)
        if mid == "first":
            started.set()
            await release.wait()

    monkeypatch.setattr(inbox, "process_max_update", handle)
    task = asyncio.create_task(inbox.process_one())
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        await asyncio.wait_for(inbox.enqueue(event("first")), timeout=5)
        assert await inbox.process_one()
        assert seen == ["first", "other"]
        assert not await inbox.process_one()
    finally:
        release.set()
        await task
    assert await inbox.process_one()
    assert seen == ["first", "other", "second"]


@pytest.mark.asyncio
async def test_cancelled_worker_leaves_event_for_restart(db, monkeypatch):
    await inbox.enqueue(event())
    started = asyncio.Event()

    async def stopped(*a, **kw):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(inbox, "process_max_update", stopped)
    worker = asyncio.create_task(inbox.process_one())
    await asyncio.wait_for(started.wait(), timeout=5)
    worker.cancel()
    with pytest.raises(asyncio.CancelledError):
        await worker
    handler = AsyncMock()
    monkeypatch.setattr(inbox, "process_max_update", handler)
    assert await inbox.process_one()
    handler.assert_awaited_once()


@pytest.mark.asyncio
async def test_failed_event_retries_and_blocks_later_user_events(db, monkeypatch):
    await inbox.enqueue(event("first"))
    await inbox.enqueue(event("second"))
    monkeypatch.setattr(inbox, "process_max_update", AsyncMock(side_effect=RuntimeError("private text")))
    for attempt in range(1, inbox.MAX_ATTEMPTS + 1):
        assert await inbox.process_one()
        assert not await inbox.process_one()
        async with db() as s:
            row = await s.scalar(select(MaxUpdate).order_by(MaxUpdate.id))
            assert row.attempts == attempt and row.error == "RuntimeError"
            assert row.payload and row.done_at is None
            if attempt < inbox.MAX_ATTEMPTS:
                row.available_at = inbox.utcnow()
            else:
                assert row.status == "failed"
            await s.commit()
    # Оператор исправил причину и повторно поставил ошибочное событие в очередь.
    async with db() as s:
        await s.execute(
            update(MaxUpdate)
            .where(MaxUpdate.status == "failed")
            .values(status="pending", attempts=0, available_at=inbox.utcnow())
        )
        await s.commit()
    monkeypatch.setattr(inbox, "process_max_update", AsyncMock())
    assert await inbox.process_one() and await inbox.process_one()


def test_identity_distinguishes_clicks_and_starts():
    cb = {"update_type": "message_callback", "callback": {"user": {"user_id": 1}, "callback_id": "one"}}
    key = inbox.identity(cb)
    cb["callback"]["callback_id"] = "two"
    assert inbox.identity(cb) != key
    start = {"update_type": "bot_started", "user": {"user_id": 1}, "timestamp": 1, "chat_id": 1}
    key = inbox.identity(start)
    start["timestamp"] = 2
    assert inbox.identity(start) != key


@pytest.mark.asyncio
async def test_max_errors_reach_queue(monkeypatch):
    monkeypatch.setattr(updates, "_employee_by_user", AsyncMock(return_value=1))
    monkeypatch.setattr(
        updates, "handle_employee_event", AsyncMock(side_effect=RuntimeError("handler failed"))
    )
    with pytest.raises(RuntimeError, match="handler failed"):
        await updates.process_max_update(event(), raise_errors=True)
    await updates.process_max_update(event())  # прежний контракт демо/поллинга сохранён


@pytest.mark.asyncio
async def test_killed_process_releases_claim(db, monkeypatch):
    await inbox.enqueue(event())
    async with db() as s:
        schema = await s.scalar(text("SELECT current_schema()"))
    script = """
import asyncio, sys
from contextlib import asynccontextmanager
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from app.bot import inbox
engine = create_async_engine(sys.argv[1], connect_args={"server_settings": {"search_path": sys.argv[2]}})
factory = async_sessionmaker(engine)
@asynccontextmanager
async def session():
    async with factory() as s:
        yield s
async def handle(*a, **kw):
    print("claimed", flush=True)
    await asyncio.Event().wait()
inbox.session, inbox.process_max_update = session, handle
asyncio.run(inbox.process_one())
"""
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-c", script, os.environ["TEST_DATABASE_URL"], schema, stdout=asyncio.subprocess.PIPE
    )
    try:
        assert await asyncio.wait_for(proc.stdout.readline(), timeout=10) == b"claimed\n"
    finally:
        if proc.returncode is None:
            proc.kill()
        await proc.wait()
    handler = AsyncMock()
    monkeypatch.setattr(inbox, "process_max_update", handler)
    assert await inbox.process_one()
    handler.assert_awaited_once()


@pytest.mark.asyncio
async def test_dialog_errors_are_not_swallowed_in_queue_mode(monkeypatch):
    @asynccontextmanager
    async def session():
        yield SimpleNamespace(get=AsyncMock(return_value=SimpleNamespace(id=1)))

    monkeypatch.setattr(updates, "session", session)
    monkeypatch.setattr(updates.dialog, "handle", AsyncMock(side_effect=RuntimeError("dialog failed")))
    with pytest.raises(RuntimeError, match="dialog failed"):
        await updates.handle_employee_event(1, updates.InEvent("text"), raise_errors=True)


def test_event_identity_does_not_depend_on_hr_chat_setting(monkeypatch):
    monkeypatch.setattr(inbox, "settings", lambda: SimpleNamespace(max_hr_chat_id=None))
    key, actor = inbox.identity(event())
    monkeypatch.setattr(inbox, "settings", lambda: SimpleNamespace(max_hr_chat_id=1))
    hr_key, hr_actor = inbox.identity(event())
    assert key == hr_key and actor != hr_actor
