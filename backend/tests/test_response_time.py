"""Срок первого ответа HR: рабочие часы площадки, однократные уведомления и экран «Сегодня»."""

import asyncio
import os
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, time, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4
from zoneinfo import ZoneInfo

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app import channels, scheduler
from app.api import admin
from app.bot import escalation, response_time
from app.bot.scenario import prev_workday, scenario
from app.db import Base, get_session
from app.models import Employee, Job, Message, Site, Staff, Ticket, TicketDeadline

MSK, YEKT = ZoneInfo("Europe/Moscow"), ZoneInfo("Asia/Yekaterinburg")
RULES = {
    "hours": 4,
    "urgent_hours": 1,
    "day_start": "09:00",
    "day_end": ["18:00", "18:00", "18:00", "18:00", "17:00"],
}


@pytest.fixture(autouse=True)
def rules(monkeypatch):
    # Алгоритм проверяется на фиксированных правилах: настройку в сценарии команда может поменять.
    monkeypatch.setattr(response_time, "rules", lambda: RULES)


def at(tz, *args):
    return datetime(*args, tzinfo=tz)


def test_scenario_has_team_setting():
    cfg = scenario()["response_time"]
    assert 0 < cfg["urgent_hours"] < cfg["hours"]
    assert len(cfg["day_end"]) == 5


@pytest.mark.parametrize(
    ("created", "severity", "due"),
    [
        (at(MSK, 2026, 10, 26, 10, 0), "medium", at(MSK, 2026, 10, 26, 14, 0)),  # понедельник, в тот же день
        (at(MSK, 2026, 10, 26, 16, 0), "medium", at(MSK, 2026, 10, 27, 11, 0)),  # остаток — утром вторника
        (at(MSK, 2026, 10, 26, 23, 0), "low", at(MSK, 2026, 10, 27, 13, 0)),  # ночь не считается
        (
            at(MSK, 2026, 10, 30, 15, 0),
            "medium",
            at(MSK, 2026, 11, 2, 11, 0),
        ),  # пятница до 17:00, потом понедельник
        (at(MSK, 2026, 10, 24, 12, 0), "medium", at(MSK, 2026, 10, 26, 13, 0)),  # выходные не считаются
        (at(MSK, 2026, 11, 3, 17, 0), "medium", at(MSK, 2026, 11, 5, 12, 0)),  # 4 ноября — праздник
        (at(MSK, 2026, 10, 26, 11, 30), "high", at(MSK, 2026, 10, 26, 12, 30)),  # срочное — 1 рабочий час
        (at(MSK, 2026, 10, 26, 23, 0), "high", at(MSK, 2026, 10, 27, 10, 0)),  # и оно не будит ночью
    ],
)
def test_deadline_counts_only_working_hours(created, severity, due):
    assert response_time.deadline(created, severity, MSK) == due


def test_deadline_uses_site_time_zone():
    night = datetime(2026, 10, 26, 20, 0, tzinfo=UTC)  # 23:00 в Москве, 01:00 вторника в Екатеринбурге
    assert response_time.deadline(night, "medium", MSK) == at(MSK, 2026, 10, 27, 13, 0)
    assert response_time.deadline(night, "medium", YEKT) == at(YEKT, 2026, 10, 27, 13, 0)
    morning = datetime(
        2026, 10, 26, 5, 30, tzinfo=UTC
    )  # 8:30 в Москве — до начала дня, 10:30 в Екатеринбурге
    assert response_time.deadline(morning, "high", MSK) == at(MSK, 2026, 10, 26, 10, 0)
    assert response_time.deadline(morning, "high", YEKT) == at(YEKT, 2026, 10, 26, 11, 30)


def test_first_reply_time_counts_only_working_hours():
    friday, monday = at(MSK, 2026, 10, 30, 16, 0), at(MSK, 2026, 11, 2, 10, 0)
    assert response_time.work_time(friday, monday, MSK) == timedelta(hours=2)
    assert response_time.work_time(
        at(MSK, 2026, 10, 26, 22, 0), at(MSK, 2026, 10, 26, 23, 0), MSK
    ) == timedelta(0)


# --- очередь и панель на PostgreSQL -------------------------------------------------------------------


@pytest_asyncio.fixture
async def db(monkeypatch):
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL нужен для проверки очереди PostgreSQL")
    schema = "response_test_" + uuid4().hex
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
                        Site.__table__,
                        Staff.__table__,
                        Employee.__table__,
                        Ticket.__table__,
                        Message.__table__,
                        Job.__table__,
                        TicketDeadline.__table__,
                    ],
                )
            )

        @asynccontextmanager
        async def session():
            async with factory() as s:
                yield s

        monkeypatch.setattr(scheduler, "session", session)
        monkeypatch.setattr(scheduler, "_job_slots", asyncio.Semaphore(2))
        client = SimpleNamespace(send=AsyncMock(return_value="mid"))
        monkeypatch.setattr(channels, "max_client", lambda: client)
        monkeypatch.setattr(channels, "settings", lambda: SimpleNamespace(max_hr_chat_id=99))
        handoff = escalation.Handoff(summary="Демо-вопрос о пропуске", suggested_action="Ответить")
        monkeypatch.setattr(escalation, "_handoff", AsyncMock(return_value=handoff))
        async with factory() as s:
            s.add(Site(code="GTP", name="Демо", kind="plant", tz="Asia/Yekaterinburg"))
            s.add_all(
                [
                    Staff(
                        id=1, name="Демо рекрутер", role="recruiter", max_user_id=7, invite_code="recruiter"
                    ),
                    Staff(id=2, name="Демо HR-руководитель", role="head", max_user_id=9, invite_code="head"),
                ]
            )
            await s.flush()
            s.add(
                Employee(
                    id=1,
                    full_name="Демо Иван Петров",
                    first_name="Иван",
                    site_code="GTP",
                    recruiter_id=1,
                    start_date=date(2026, 10, 26),
                    probation_end=date(2027, 1, 26),
                    invite_code="demo",
                    state={},
                )
            )
            await s.commit()
        yield SimpleNamespace(session=factory, client=client)
    finally:
        await engine.dispose()
        async with owner.begin() as c:
            await c.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await owner.dispose()


async def create(db, kind="question", severity="medium") -> int:
    async with db.session() as s:
        t = await escalation.create_ticket(
            s, await s.get(Employee, 1), kind, "Демо: где получить пропуск?", severity
        )
        return t.id


async def make_due(db, tid):
    past = scheduler.utcnow() - timedelta(minutes=1)
    async with db.session() as s:
        await s.execute(update(TicketDeadline).where(TicketDeadline.ticket_id == tid).values(due_at=past))
        await s.execute(
            update(Job)
            .where(Job.kind == response_time.KIND, Job.ref == str(tid), Job.status == "pending")
            .values(run_at=past)
        )
        await s.commit()


def sent_to(db, user_id) -> list[str]:
    return [c.args[0].text for c in db.client.send.await_args_list if c.kwargs.get("user_id") == user_id]


async def api(db, method, path):
    app = FastAPI()
    app.include_router(admin.router)
    app.dependency_overrides[admin.auth] = lambda: None

    async def session_override():
        async with db.session() as s:
            yield s

    app.dependency_overrides[get_session] = session_override
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        return await client.request(method, path)


@pytest.mark.asyncio
async def test_deadline_and_check_are_saved_with_ticket(db):
    before = scheduler.utcnow()
    tid = await create(db)
    async with db.session() as s:
        d = await s.get(TicketDeadline, tid)
        jobs = list(await s.scalars(select(Job).where(Job.kind == response_time.KIND)))
    assert response_time.deadline(before, "medium", YEKT) <= d.due_at
    assert d.due_at <= response_time.deadline(scheduler.utcnow(), "medium", YEKT)
    assert [(j.ref, j.run_at, j.employee_id) for j in jobs] == [(str(tid), d.due_at, None)]


@pytest.mark.asyncio
async def test_overdue_reaches_recruiter_and_head_once(db):
    tid = await create(db)
    assert await scheduler.tick() == 1  # карточка HR; срок ещё не вышел
    db.client.send.reset_mock()
    await make_due(db, tid)
    assert await scheduler.tick() == 1
    assert len(sent_to(db, 7)) == 1 and "ждёт ответа" in sent_to(db, 7)[0]
    assert len(sent_to(db, 9)) == 1 and f"#{tid}" in sent_to(db, 9)[0]
    assert await scheduler.tick() == 0
    # Задачу подобрали снова (перезапуск до отметки «выполнено»): отметки срока не дают повторить уведомление.
    async with db.session() as s:
        job = await s.scalar(select(Job).where(Job.kind == response_time.KIND))
        job.status = "running"
        await s.commit()
    assert await scheduler.tick() == 1
    assert db.client.send.await_count == 2
    async with db.session() as s:
        d = await s.get(TicketDeadline, tid)
        assert d.overdue_at and d.reminded_at and d.reminder_delivered is True


@pytest.mark.asyncio
async def test_urgent_reaches_head_at_once_without_second_recruiter_message(db):
    tid = await create(db, "alert", "high")
    assert await scheduler.tick() == 2  # карточка HR и срочное уведомление
    assert len(sent_to(db, 7)) == 1  # рекрутеру — только карточка из escalation.finalize
    assert len(sent_to(db, 9)) == 1 and "Срочное" in sent_to(db, 9)[0]
    await make_due(db, tid)
    assert await scheduler.tick() == 1
    assert len(sent_to(db, 7)) == 2 and len(sent_to(db, 9)) == 2


@pytest.mark.asyncio
async def test_failure_while_sending_does_not_repeat_notification(db, monkeypatch):
    tid = await create(db)
    assert await scheduler.tick() == 1
    await make_due(db, tid)
    # Сбой посреди отправки: отметки уже сохранены, поэтому повтор задачи уведомление не дублирует.
    monkeypatch.setattr(channels, "notify_staff", AsyncMock(side_effect=RuntimeError("сбой отправки")))
    assert await scheduler.tick() == 1
    async with db.session() as s:
        job = await s.scalar(select(Job).where(Job.kind == response_time.KIND))
        assert job.status == "pending" and job.attempts == 1
        job.run_at = scheduler.utcnow()
        await s.commit()
    notify = AsyncMock(return_value=True)
    monkeypatch.setattr(channels, "notify_staff", notify)
    assert await scheduler.tick() == 1
    notify.assert_not_awaited()


@pytest.mark.asyncio
async def test_without_recruiter_head_is_told_and_no_reminder_is_shown(db):
    async with db.session() as s:
        (await s.get(Employee, 1)).recruiter_id = None
        await s.commit()
    tid = await create(db)
    assert await scheduler.tick() == 1
    await make_due(db, tid)
    assert await scheduler.tick() == 1
    assert len(sent_to(db, 9)) == 1 and "не назначен" in sent_to(db, 9)[0]
    async with db.session() as s:
        d = await s.get(TicketDeadline, tid)
        assert d.overdue_at and d.reminded_at is None and d.reminder_delivered is None
    response = await api(db, "POST", f"/api/tickets/{tid}/remind")
    assert response.status_code == 409 and "не назначен" in response.json()["detail"]


@pytest.mark.asyncio
async def test_head_who_leads_newcomer_gets_no_duplicates(db):
    async with db.session() as s:
        (await s.get(Employee, 1)).recruiter_id = 2  # HR-руководитель сам ведёт новичка
        await s.commit()
    tid = await create(db, "alert", "high")
    assert await scheduler.tick() == 2
    assert len(sent_to(db, 9)) == 1  # карточка рекрутеру, без второго «срочного»
    await make_due(db, tid)
    assert await scheduler.tick() == 1
    assert len(sent_to(db, 9)) == 2  # и одно напоминание после срока


@pytest.mark.asyncio
async def test_hr_reply_clears_overdue(db):
    tid = await create(db)
    assert await scheduler.tick() == 1
    db.client.send.reset_mock()
    async with db.session() as s:
        await escalation.hr_reply(s, await s.get(Ticket, tid), "Пропуск выдадут на проходной", "Мария")
    await make_due(db, tid)
    assert await scheduler.tick() == 1
    db.client.send.assert_not_awaited()  # сотрудник в демо-канале, уведомлений HR нет
    async with db.session() as s:
        view = (await response_time.views(s, [await s.get(Ticket, tid)]))[tid]
    assert view["first_reply_at"] and not view["overdue"]
    assert (await api(db, "GET", "/api/response-time")).json()["overdue"] == []


@pytest.mark.asyncio
async def test_closed_ticket_does_not_remind(db):
    tid = await create(db)
    assert await scheduler.tick() == 1
    async with db.session() as s:
        await escalation.close(s, await s.get(Ticket, tid), notify=False)
    db.client.send.reset_mock()
    await make_due(db, tid)
    assert await scheduler.tick() == 1
    db.client.send.assert_not_awaited()
    assert (await api(db, "POST", f"/api/tickets/{tid}/remind")).status_code == 409


@pytest.mark.asyncio
async def test_remind_button_reports_delivery(db):
    tid = await create(db)
    response = await api(db, "POST", f"/api/tickets/{tid}/remind")
    assert response.status_code == 200 and response.json() == {"delivered": True}
    assert len(sent_to(db, 7)) == 1
    async with db.session() as s:
        (await s.get(Staff, 1)).max_user_id = None
        await s.commit()
    assert (await api(db, "POST", f"/api/tickets/{tid}/remind")).json() == {"delivered": False}
    tickets = (await api(db, "GET", "/api/tickets?status=open")).json()
    assert tickets[0]["response"]["recruiter"] == "Демо рекрутер"
    assert tickets[0]["response"]["reminder_delivered"] is False
    ticket = (await api(db, "GET", f"/api/tickets/{tid}")).json()
    assert ticket["response"]["recruiter"] == "Демо рекрутер" and not ticket["response"]["overdue"]


@pytest.mark.asyncio
async def test_today_shows_overdue_and_first_reply_median(db):
    empty = (await api(db, "GET", "/api/response-time")).json()
    assert empty["overdue"] == [] and empty["first_reply"]["median_min"] is None
    # Обращение до появления сроков: срок считается от времени создания (15 дней перекрывают новогодние праздники).
    async with db.session() as s:
        s.add(
            Ticket(
                id=50,
                employee_id=1,
                kind="question",
                subject="Старое обращение",
                created_at=scheduler.utcnow() - timedelta(days=15),
            )
        )
        await s.commit()
    # Два отвеченных обращения в рабочий день площадки: через 30 и через 90 минут.
    day = prev_workday(datetime.now(YEKT).date() - timedelta(days=2))
    for tid, minutes in ((51, 30), (52, 90)):
        created = datetime.combine(day, time(10, 0), tzinfo=YEKT)
        async with db.session() as s:
            s.add(
                Ticket(
                    id=tid,
                    employee_id=1,
                    kind="question",
                    subject="Демо",
                    status="closed",
                    created_at=created,
                )
            )
            await s.flush()
            s.add(
                Message(
                    employee_id=1,
                    ticket_id=tid,
                    direction="out",
                    author="hr",
                    text="Ответ",
                    created_at=created + timedelta(minutes=minutes),
                )
            )
            await s.commit()
    today = (await api(db, "GET", "/api/response-time")).json()
    assert [t["id"] for t in today["overdue"]] == [50]
    assert today["overdue"][0]["response"]["recruiter"] == "Демо рекрутер"
    assert today["first_reply"] == {"median_min": 60, "tickets": 2, "days": 30}
    assert today["rules"] == {"hours": 4, "urgent_hours": 1}
