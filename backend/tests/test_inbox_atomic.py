"""Сбой входящего события не оставляет частично сохранённый ответ или исходящее сообщение."""

import asyncio
import os
from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app import channels, scheduler
from app.analytics import analysis_queue
from app.api import admin
from app.bot import dialog, escalation, inbox, surveys, updates
from app.db import Base
from app.models import (
    Analysis,
    AnalysisTask,
    Employee,
    Job,
    MaxDelivery,
    MaxUpdate,
    Message,
    Question,
    Site,
    Staff,
    SurveyAnswer,
    Ticket,
    TicketDeadline,
)


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
                            Analysis,
                            AnalysisTask,
                            Question,
                            TicketDeadline,
                        )
                    ],
                )
            )

        @asynccontextmanager
        async def session():
            async with factory() as s:
                yield s

        monkeypatch.setattr(inbox, "session", session)
        monkeypatch.setattr(analysis_queue, "session", session)
        monkeypatch.setattr(updates, "session", session)
        monkeypatch.setattr(updates, "_locks", defaultdict(asyncio.Lock))
        monkeypatch.setattr(updates.demo_try, "schedule_after_event", Mock())
        monkeypatch.setattr(updates.demo_try, "schedule_hint", Mock())
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
        yield SimpleNamespace(session=factory, client=client, schema=schema)
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


@pytest.mark.asyncio
async def test_dialog_lock_is_held_until_inbox_commit(db, monkeypatch):
    started, release, competitor_started = asyncio.Event(), asyncio.Event(), asyncio.Event()
    process, handle = inbox.process_max_update, dialog.handle
    seen = []

    async def pause(*args, **kwargs):
        await process(*args, **kwargs)
        started.set()
        await release.wait()

    async def observe(s, emp, ev):
        if ev.kind == "text":
            seen.append(emp.state["survey"]["q"])
        else:
            await handle(s, emp, ev)

    async def compete():
        competitor_started.set()
        await updates.handle_employee_event(1, channels.InEvent("text", text="/menu"), raise_errors=True)

    monkeypatch.setattr(inbox, "process_max_update", pause)
    monkeypatch.setattr(dialog, "handle", observe)
    await inbox.enqueue(callback())
    worker = asyncio.create_task(inbox.process_one())
    competitor = None
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        assert updates._locks[1].locked()
        competitor = asyncio.create_task(compete())
        await asyncio.wait_for(competitor_started.wait(), timeout=5)
        assert updates._locks[1].locked() and not competitor.done() and not seen
        release.set()
        assert await asyncio.wait_for(worker, timeout=5)
        await asyncio.wait_for(competitor, timeout=5)
        assert seen == [1]
    finally:
        release.set()
        tasks = [task for task in (worker, competitor) if task is not None]
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("fail", [False, True])
async def test_comment_analysis_starts_only_after_commit(db, monkeypatch, fail):
    async with db.session() as s:
        ans = SurveyAnswer(employee_id=1, survey_id="day3", question_id="docs", value={"option": "c"})
        msg = Message(employee_id=1, direction="in", author="employee", text="Нужна помощь с доступом")
        s.add_all([ans, msg])
        await s.flush()
        emp = await s.get(Employee, 1)
        emp.state = {
            **emp.state,
            "await": {"kind": "survey_comment", "answer_id": ans.id, "candidate_id": msg.id, "qid": "docs"},
        }
        await s.commit()
    analysis = Mock()
    monkeypatch.setattr(surveys, "analyze_later", analysis)
    process = inbox.process_max_update

    async def finish(*args, **kwargs):
        await process(*args, **kwargs)
        analysis.assert_not_called()
        if fail:
            raise RuntimeError("interrupted before inbox commit")

    monkeypatch.setattr(inbox, "process_max_update", finish)
    await inbox.enqueue(callback(f"a:confirm_comment:{msg.id}"))
    assert await inbox.process_one()
    if fail:
        analysis.assert_not_called()
        async with db.session() as s:
            assert (await s.get(SurveyAnswer, ans.id)).comment is None
    else:
        analysis.assert_called_once_with(1, msg.text, "survey", None)
        async with db.session() as s:
            assert (await s.get(SurveyAnswer, ans.id)).comment == msg.text


@pytest.mark.asyncio
async def test_start_waits_before_binding_and_preserves_concurrent_state(db, monkeypatch):
    async with db.session() as s:
        emp = await s.get(Employee, 1)
        emp.channel, emp.max_user_id = "web", None
        emp.state = {"pressed": ["welcome:0"]}
        await s.commit()
    waiting = asyncio.Event()
    lock_employee = updates.EventContext.lock_employee

    async def observe_wait(context, employee_id):
        waiting.set()
        await lock_employee(context, employee_id)

    monkeypatch.setattr(updates.EventContext, "lock_employee", observe_wait)
    await inbox.enqueue(
        {
            "update_type": "bot_started",
            "user": {"user_id": 1},
            "chat_id": 1,
            "timestamp": 1,
            "payload": "demo",
        }
    )
    worker = None
    try:
        async with updates._locks[1]:
            worker = asyncio.create_task(inbox.process_one())
            await asyncio.wait_for(waiting.wait(), timeout=5)
            async with db.session() as s:
                emp = await s.get(Employee, 1)
                assert emp.channel == "web" and emp.max_user_id is None
                emp.state = {**emp.state, "last_problem": "Нужны доступы"}
                await asyncio.wait_for(s.commit(), timeout=5)
        assert await asyncio.wait_for(worker, timeout=5)
        async with db.session() as s:
            emp = await s.get(Employee, 1)
            assert emp.channel == "max" and emp.max_user_id == 1
            assert emp.state == {"pressed": [], "last_problem": "Нужны доступы"}
            assert await s.scalar(select(func.count()).select_from(MaxDelivery)) == 2
            assert (await s.scalar(select(MaxUpdate))).status == "done"
    finally:
        if worker and not worker.done():
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)


async def hr_ticket(db, *, active=False):
    async with db.session() as s:
        ticket = Ticket(employee_id=1, kind="problem", subject="Нужны доступы", hr_chat_mid="hr-card")
        s.add(ticket)
        await s.flush()
        if active:
            emp = await s.get(Employee, 1)
            emp.state = {**emp.state, "ticket_id": ticket.id}
        await s.commit()
        return ticket.id


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["reply", "close"])
@pytest.mark.parametrize("transport", ["api", "polling"])
async def test_hr_action_preserves_concurrent_survey_answer(db, monkeypatch, action, transport):
    ticket_id = await hr_ticket(db, active=action == "close")
    saved, release, hr_started = asyncio.Event(), asyncio.Event(), asyncio.Event()
    process = inbox.process_max_update

    async def pause(*args, **kwargs):
        await process(*args, **kwargs)
        saved.set()
        await release.wait()

    monkeypatch.setattr(inbox, "process_max_update", pause)
    tasks = []
    async with db.session() as hr:
        stale = await hr.get(Ticket, ticket_id)
        assert stale.employee.state["survey"]["q"] == 0

        @asynccontextmanager
        async def hr_session():
            yield hr

        if transport == "polling":
            monkeypatch.setattr(updates, "session", hr_session)

        async def act():
            hr_started.set()
            if transport == "polling":
                msg = {"link": {"type": "reply", "message": {"mid": "hr-card"}}}
                await updates._on_hr_chat(msg, "/close" if action == "close" else "Поможем с доступами")
            elif action == "close":
                await admin.close_ticket(ticket_id, hr)
            else:
                await admin.reply_ticket(ticket_id, admin.ReplyIn(text="Поможем с доступами"), hr)

        try:
            await inbox.enqueue(callback())
            worker = asyncio.create_task(inbox.process_one())
            tasks.append(worker)
            await asyncio.wait_for(saved.wait(), timeout=5)
            request = asyncio.create_task(act())
            tasks.append(request)
            await asyncio.wait_for(hr_started.wait(), timeout=5)
            assert not request.done()
            release.set()
            await asyncio.wait_for(asyncio.gather(*tasks), timeout=5)
        finally:
            release.set()
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
    async with db.session() as s:
        emp = await s.get(Employee, 1)
        assert emp.state["survey"]["q"] == 1
        assert emp.state.get("ticket_id") == (ticket_id if action == "reply" else None)
        assert (await s.get(Ticket, ticket_id)).status == ("open" if action == "reply" else "closed")
        assert await s.scalar(select(func.count()).select_from(SurveyAnswer)) == 1
        assert await s.scalar(select(func.count()).select_from(MaxDelivery)) == 2
        assert (await s.scalar(select(MaxUpdate))).status == "done"


@pytest.mark.asyncio
async def test_hr_reply_rereads_ticket_closed_under_scheduler_lock(db, monkeypatch):
    ticket_id = await hr_ticket(db, active=True)
    entered = asyncio.Event()
    read_ticket = admin._ticket

    async def observe_read(s, tid):
        ticket = await read_ticket(s, tid)
        entered.set()
        return ticket

    monkeypatch.setattr(admin, "_ticket", observe_read)
    request = None
    async with db.session() as hr:
        stale = await hr.get(Ticket, ticket_id)
        assert stale.status == "open"
        try:
            async with db.session() as guard, guard.begin(), scheduler.employee_owner(guard, 1) as owned:
                assert owned
                request = asyncio.create_task(
                    admin.reply_ticket(ticket_id, admin.ReplyIn(text="Поздний ответ"), hr)
                )
                await asyncio.wait_for(entered.wait(), timeout=5)
                async with db.session() as closer:
                    ticket = await closer.get(Ticket, ticket_id)
                    ticket.employee.state = {
                        **ticket.employee.state,
                        "survey": {"id": "day3", "q": 1, "key": "run"},
                    }
                    await asyncio.wait_for(escalation.close(closer, ticket, notify=False), timeout=5)
            with pytest.raises(HTTPException) as error:
                await asyncio.wait_for(request, timeout=5)
            assert error.value.status_code == 409
        finally:
            if request and not request.done():
                request.cancel()
            if request:
                await asyncio.gather(request, return_exceptions=True)
    async with db.session() as s:
        assert await s.scalar(select(func.count()).select_from(MaxDelivery)) == 0
        assert await s.scalar(select(func.count()).select_from(Message)) == 0
        assert (await s.get(Employee, 1)).state["survey"]["q"] == 1


@pytest.mark.asyncio
async def test_waiting_hr_releases_connection_for_dialog_owner(db, monkeypatch):
    ticket_id = await hr_ticket(db)
    waiting = asyncio.Event()

    class ObservedLock(asyncio.Lock):
        async def acquire(self):
            waiting.set()
            return await super().acquire()

    monkeypatch.setitem(updates._locks, 1, ObservedLock())
    engine = create_async_engine(
        os.environ["TEST_DATABASE_URL"],
        pool_size=1,
        max_overflow=0,
        pool_timeout=1,
        connect_args={"server_settings": {"search_path": db.schema}},
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    request = None
    try:
        async with factory() as owner, factory() as hr:
            try:
                async with updates._locks[1]:
                    waiting.clear()
                    emp = await owner.get(Employee, 1)
                    await owner.commit()
                    request = asyncio.create_task(
                        admin.reply_ticket(ticket_id, admin.ReplyIn(text="Поможем с доступами"), hr)
                    )
                    await asyncio.wait_for(waiting.wait(), timeout=5)
                    assert not request.done()
                    # Владелец диалога продолжает работу после промежуточного commit.
                    await owner.refresh(emp)
                    emp.state = {**emp.state, "survey": {"id": "day3", "q": 1, "key": "run"}}
                    await owner.commit()
                await asyncio.wait_for(request, timeout=5)
            finally:
                if request and not request.done():
                    request.cancel()
                if request:
                    await asyncio.gather(request, return_exceptions=True)
    finally:
        await engine.dispose()
    async with db.session() as s:
        emp = await s.get(Employee, 1)
        assert emp.state["survey"]["q"] == 1
        assert emp.state["ticket_id"] == ticket_id
        assert await s.scalar(select(func.count()).select_from(MaxDelivery)) == 1
