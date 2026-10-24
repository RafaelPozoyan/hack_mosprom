"""Плановое время сообщений в демо-чате: формулировка, часовой пояс площадки и совпадение с планом."""

import asyncio
import json
import os
import re
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app import channels, scheduler
from app.api import admin, demo
from app.bot import plan_time, scenario
from app.channels import outbox
from app.db import Base
from app.models import (
    Analysis,
    Employee,
    Job,
    MaxDelivery,
    Message,
    Site,
    Staff,
    StepAttachment,
    SurveyAnswer,
    Ticket,
)

WEEKDAYS = {0: "пн", 1: "вт", 2: "ср", 3: "чт", 4: "пт", 5: "сб", 6: "вс"}
TAIL = re.compile(r"(\d\d):(\d\d) · (пн|вт|ср|чт|пт|сб|вс) (\d\d)\.(\d\d)$")


def newcomer(site="MSK", start=date(2026, 11, 9), **kw):
    base = {"site_code": site, "start_date": start, "probation_end": date(2027, 2, 9), "state": {}}
    return SimpleNamespace(**(base | kw))


def clock(caption):
    """(часы, минуты, число, месяц, день недели) из хвоста подписи — как её прочитает человек."""
    hour, minute, weekday, day, month = TAIL.search(caption).groups()
    return int(hour), int(minute), int(day), int(month), weekday


def planned(when):
    return when.hour, when.minute, when.day, when.month, WEEKDAYS[when.weekday()]


# --- формулировка расписания -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("n", "word"),
    [
        (1, "день"),
        (2, "дня"),
        (4, "дня"),
        (5, "дней"),
        (11, "дней"),
        (12, "дней"),
        (21, "день"),
        (111, "дней"),
    ],
)
def test_plural_uses_the_form_the_number_requires(n, word):
    assert plan_time.plural(n, "день", "дня", "дней") == f"{n} {word}"


@pytest.mark.parametrize(
    ("at", "words"),
    [
        ({"event": "start"}, "сразу после запуска по QR"),
        ({"day": -1, "time": "10:00"}, "за 1 день до выхода"),
        ({"day": -2}, "за 2 дня до выхода"),
        ({"day": -5}, "за 5 дней до выхода"),
        ({"day": 0}, "в день выхода"),
        ({"day": 3}, "3 дня после выхода"),
        ({"workday": 1}, "1-й рабочий день"),
        ({"workday": 3, "time": "15:00"}, "3-й рабочий день"),
        ({"days": 10}, "10 дней после выхода"),
        ({"weeks": 1}, "1 неделя после выхода"),
        ({"weeks": 2}, "2 недели после выхода"),
        ({"weeks": 5}, "5 недель после выхода"),
        ({"months": 1}, "1 месяц после выхода"),
        ({"months": 1, "weeks": 2}, "1 месяц и 2 недели после выхода"),
        ({"months": 2, "weeks": 2, "days": 3}, "2 месяца, 2 недели и 3 дня после выхода"),
        ({"month_start": 2}, "начало 2-го месяца работы"),
        ({"probation_end": True}, "конец испытательного срока"),
        ({"time": "10:00"}, None),
    ],
)
def test_schedule_is_described_in_plain_words(at, words):
    assert plan_time.schedule_words(at) == words


def test_every_schedule_in_the_scenario_has_wording():
    # Новый вид `at` в сценарии без формулировки оставил бы шаг без подписи в демо-чате.
    for st in scenario.scenario()["steps"]:
        if st.get("at"):
            assert plan_time.schedule_words(st["at"]), st["id"]


# --- подпись: время, день недели и дата площадки -----------------------------------------------------


@pytest.fixture
def tiny(monkeypatch):
    """Маленький сценарий со всеми способами запуска шага: время, then, then опроса, кнопка шага, меню, команда."""

    def button(goto):
        return [{"text": "Кнопка", "goto": goto}]

    data = {
        "sites": {"MSK": {"name": "Москва"}, "VLG": {"name": "Владивосток", "tz": "Asia/Vladivostok"}},
        "menu": {"buttons": button("shared") + button("faq")},
        "commands": {"start": "menu", "food": "lunch"},
        "surveys": {"mood": {"title": "Настроение", "then": "bye"}},
        "steps": [
            {
                "id": "hello",
                "title": "Знакомство",
                "at": {"event": "start"},
                "messages": [{"text": "Привет", "buttons": button("intro")}],
            },
            {
                "id": "intro",
                "title": "Что умеет бот",
                "then": ["docs", "shared"],
                "messages": [{"text": "Умею"}],
            },
            {"id": "docs", "title": "Документы", "messages": [{"text": "Документы"}]},
            {"id": "shared", "title": "Справка", "messages": [{"text": "Справка"}]},
            {
                "id": "eve",
                "title": "Накануне",
                "at": {"day": -1, "time": "10:00"},
                "then": ["note"],
                "messages": [{"text": "Завтра выход"}],
            },
            {
                "id": "note",
                "title": "Напоминание",
                "messages": [{"text": "Документы", "buttons": button("shared")}],
            },
            {
                "id": "first",
                "title": "Первый день",
                "at": {"workday": 1, "time": "09:00"},
                "messages": [{"text": "Привет", "buttons": button("map") + button("lunch") + button("faq")}],
            },
            {"id": "map", "title": "Карта", "messages": [{"text": "Карта"}]},
            {"id": "lunch", "title": "Где пообедать", "messages": [{"text": "Обед"}]},
            {"id": "faq", "title": "Вопросы", "messages": [{"text": "Вопросы"}]},
            {"id": "second", "title": "Второй день", "at": {"workday": 2, "time": "10:00"}, "messages": []},
            {
                "id": "check",
                "title": "Опрос",
                "at": {"weeks": 2, "time": "10:00"},
                "messages": [{"survey": "mood"}],
            },
            {"id": "bye", "title": "Прощание", "messages": [{"text": "Пока"}]},
            {"id": "end", "title": "Итоги", "at": {"probation_end": True, "time": "18:00"}, "messages": []},
        ],
    }
    monkeypatch.setattr(scenario, "scenario", lambda: data)
    monkeypatch.setattr(plan_time, "scenario", lambda: data)
    return data


def test_caption_joins_wording_time_and_date(tiny):
    caps = plan_time.captions(newcomer())
    assert caps["eve"] == "за 1 день до выхода, 10:00 · вс 08.11"
    assert caps["first"] == "1-й рабочий день, 09:00 · пн 09.11"
    assert caps["check"] == "2 недели после выхода, 10:00 · пн 23.11"
    assert caps["end"] == "конец испытательного срока, 18:00 · вт 09.02"
    assert caps["hello"] == "сразу после запуска по QR"  # дата зависит от того, когда новичок откроет QR


def test_steps_that_only_follow_a_scheduled_one_share_its_caption(tiny):
    caps = plan_time.captions(newcomer())
    assert caps["note"] == caps["eve"]  # then
    assert caps["map"] == caps["first"]  # ответ на кнопку шага
    assert caps["survey:mood"] == caps["check"]  # вопросы опроса пишутся с step_id «survey:<id>»
    assert caps["bye"] == caps["check"]  # then опроса
    assert caps["intro"] == caps["docs"] == caps["hello"]  # знакомство после «На ТЫ / На ВЫ» и его цепочка


def test_steps_that_can_be_opened_from_several_places_have_no_caption(tiny):
    caps = plan_time.captions(newcomer())
    assert (
        "shared" not in caps
    )  # then знакомства, кнопка меню и кнопка напоминания: когда его увидят — неизвестно
    assert "lunch" not in caps  # кнопка шага first и команда меню: от плана не зависит
    assert "faq" not in caps  # кнопка шага first и кнопка меню


def test_first_workday_after_a_weekend_start_is_monday(tiny):
    caps = plan_time.captions(newcomer(start=date(2026, 11, 8)))  # воскресенье
    assert caps["first"] == "1-й рабочий день, 09:00 · пн 09.11"
    assert caps["eve"] == "за 1 день до выхода, 10:00 · сб 07.11"  # накануне — календарный день, без переноса


def test_holiday_is_not_a_workday(tiny):
    caps = plan_time.captions(newcomer(start=date(2026, 11, 3)))  # 4 ноября — День народного единства
    assert caps["second"] == "2-й рабочий день, 10:00 · чт 05.11"


def test_date_and_time_follow_the_site_time_zone(tiny):
    vladivostok = newcomer("VLG")
    due = {st["id"]: run_at for st, run_at in scenario.timeline(vladivostok)}["first"]
    assert due.astimezone(UTC).date() == date(2026, 11, 8)  # по Гринвичу ещё воскресенье
    assert plan_time.captions(vladivostok)["first"] == "1-й рабочий день, 09:00 · пн 09.11"
    # Один и тот же момент у двух площадок читается по-разному: так выглядят и задачи, сохранённые в UTC.
    instant = datetime(2026, 11, 8, 23, 0, tzinfo=UTC)
    assert plan_time.moment(vladivostok, instant) == "09:00 · пн 09.11"
    assert plan_time.moment(newcomer("MSK"), instant) == "02:00 · пн 09.11"


@pytest.mark.parametrize("site", ["GrPP", "GTP", "Luzhniki"])
def test_captions_of_the_real_scenario_match_the_planned_run_time(site):
    emp = newcomer(site)
    caps = plan_time.captions(emp)
    scheduled = [(st, run_at) for st, run_at in scenario.timeline(emp) if run_at]
    assert scheduled
    for st, run_at in scheduled:
        assert clock(caps[st["id"]]) == planned(run_at.astimezone(scenario.site_tz(site)))
        assert caps[st["id"]].startswith(plan_time.schedule_words(st["at"]))


def test_real_scenario_steps_follow_the_step_that_starts_them():
    caps = plan_time.captions(newcomer("GTP"))
    for acquaintance in ("intro", "transport_offer", "company_facts", "see_you"):
        assert caps[acquaintance] == caps["welcome"] == "сразу после запуска по QR"
    assert caps["documents_reminder"] == caps["day_before"]
    assert caps["d1_postcard"] == caps["d1_access"]
    assert caps["map_info"] == caps["d1_map"]
    assert caps["survey:day3"] == caps["d3_survey"]
    assert caps["final_bye"] == caps["final"]


def test_real_scenario_reference_steps_have_no_caption():
    # «Где пообедать» и список документов открываются и шагом, и меню, и командой.
    for site in ("GrPP", "GTP", "Luzhniki"):
        caps = plan_time.captions(newcomer(site))
        assert "lunch_info" not in caps and "documents_info" not in caps
    assert "map_info" not in plan_time.captions(newcomer("GrPP"))  # карты у офиса «Группы» нет


def test_upcoming_describes_steps_and_service_jobs(tiny):
    emp = newcomer()
    due = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
    step_job = SimpleNamespace(kind="step", ref="first", run_at=due)
    assert plan_time.upcoming(emp, step_job) == {
        "title": "Первый день",
        "plan": "1-й рабочий день, 09:00 · пн 09.11",  # плановое время шага, а не срок задачи
    }
    # У напоминаний места в плане нет: срок считается от того, когда шаг реально ушёл, — времени не показываем.
    for kind, title in [
        ("reminder", "Напоминание об опросе"),
        ("required_reminder", "Напоминание об обязательном шаге"),
        ("required_escalation", "Сигнал HR: обязательный шаг не пройден"),
    ]:
        job = SimpleNamespace(kind=kind, ref="k1", run_at=due)
        assert plan_time.upcoming(emp, job) == {"title": title, "plan": None}


def test_start_hint_is_the_acquaintance_step(tiny):
    assert plan_time.start_hint(newcomer()) == {"title": "Знакомство", "plan": "сразу после запуска по QR"}


# --- демо-API на PostgreSQL --------------------------------------------------------------------------


@pytest_asyncio.fixture
async def db(monkeypatch):
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL нужен для проверки демо-чата на PostgreSQL")
    schema = "plan_time_test_" + uuid4().hex
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
                        Job.__table__,
                        StepAttachment.__table__,
                        Ticket.__table__,
                        Message.__table__,
                        MaxDelivery.__table__,
                        SurveyAnswer.__table__,
                        Analysis.__table__,
                    ],
                )
            )

        @asynccontextmanager
        async def session():
            async with factory() as s:
                yield s

        monkeypatch.setattr(scheduler, "session", session)
        monkeypatch.setattr(demo, "session", session)
        monkeypatch.setattr(outbox, "session", session)
        monkeypatch.setattr(scheduler, "_job_slots", asyncio.Semaphore(2))
        monkeypatch.setattr(outbox, "max_client", lambda: None)
        async with factory() as s:
            s.add(Site(code="GTP", name="Демо", kind="plant"))
            await s.flush()
            s.add(
                Employee(
                    id=1,
                    full_name="Демо Иван Петров",
                    first_name="Иван",
                    site_code="GTP",
                    start_date=date.today() + timedelta(days=30),
                    probation_end=date.today() + timedelta(days=120),
                    invite_code="demo",
                    status="active",
                    address_form="vy",
                    state={},
                )
            )
            await s.commit()
        yield SimpleNamespace(session=factory)
    finally:
        await engine.dispose()
        async with owner.begin() as c:
            await c.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await owner.dispose()


@pytest.mark.asyncio
async def test_caption_matches_scheduled_jobs_and_the_card_timeline(db):
    async with db.session() as s:
        emp = await s.get(Employee, 1)
        assert await scenario.schedule_employee(s, emp)
        jobs = list(await s.scalars(select(Job).where(Job.kind == "step")))
        card = await admin.employee_card(1, s)
    caps = plan_time.captions(emp)
    site = scenario.site_tz(emp.site_code)
    assert jobs
    for job in jobs:
        assert clock(caps[job.ref]) == planned(job.run_at.astimezone(site))
    # Карточка рисует плановое время по Москве (fmtDateTime в web/src/api.ts), а подпись — по поясу площадки.
    # Пока у всех площадок московское время, они совпадают; другой пояс площадки разведёт их — это надо решать.
    moscow = ZoneInfo("Europe/Moscow")
    for item in card["timeline"]:
        if item["run_at"]:
            shown = datetime.fromisoformat(item["run_at"]).astimezone(moscow)
            assert clock(caps[item["step_id"]]) == planned(shown)


@pytest.mark.asyncio
async def test_demo_messages_carry_the_plan_only_for_scenario_steps(db):
    async with db.session() as s:
        s.add_all(
            [
                Message(employee_id=1, direction="out", author="bot", text="Шаг", step_id="d1_access"),
                Message(employee_id=1, direction="out", author="bot", text="Вопрос", step_id="survey:day3"),
                Message(employee_id=1, direction="out", author="bot", text="Ответ ИИ"),
                Message(employee_id=1, direction="out", author="hr", text="Ответ HR"),
                Message(employee_id=1, direction="in", author="employee", text="Вопрос новичка"),
            ]
        )
        await s.commit()
        view = {m["text"]: m for m in await demo.messages(1, s)}
        emp = await s.get(Employee, 1)
    caps = plan_time.captions(emp)
    assert view["Шаг"]["plan"] == caps["d1_access"]
    assert view["Вопрос"]["plan"] == caps["d3_survey"]
    assert [view[t]["plan"] for t in ("Ответ ИИ", "Ответ HR", "Вопрос новичка")] == [None, None, None]


@pytest.mark.asyncio
async def test_new_and_edited_messages_reach_the_chat_stream_with_the_plan(db):
    async with db.session() as s:
        step_message = Message(employee_id=1, direction="out", author="bot", text="Шаг", step_id="d1_access")
        answer = Message(employee_id=1, direction="out", author="bot", text="Ответ ИИ")
        s.add_all([step_message, answer])
        await s.commit()
        emp = await s.get(Employee, 1)
    shared = channels.message_view(step_message)
    assert "plan" not in shared  # общий вид сообщения (журнал, обращения) подписи не содержит
    chunks = (await demo.stream(1)).body_iterator
    try:
        assert await anext(chunks) == ": connected\n\n"
        channels.hub.publish(1, {"type": "message", "message": shared})
        channels.hub.publish(1, {"type": "message", "message": channels.message_view(answer)})
        channels.hub.publish(1, {"type": "edit", "message": shared})
        first, second, edited = [
            json.loads((await asyncio.wait_for(anext(chunks), 5)).removeprefix("data: "))["message"]
            for _ in range(3)
        ]
    finally:
        await chunks.aclose()
    caption = plan_time.captions(emp)["d1_access"]
    assert first["text"] == "Шаг" and first["plan"] == caption
    assert second["text"] == "Ответ ИИ" and second["plan"] is None
    assert edited["plan"] == caption  # правка сообщения не должна терять подпись


@pytest.mark.asyncio
async def test_a_failed_caption_does_not_lose_the_chat_message(db, monkeypatch):
    def broken(emp):
        raise RuntimeError("сценарий повреждён")

    monkeypatch.setattr(plan_time, "captions", broken)
    event = {"type": "message", "message": {"id": 5, "step_id": "d1_access", "text": "Шаг"}}
    sent = await demo.with_plan(1, event)
    assert sent["message"] == {"id": 5, "step_id": "d1_access", "text": "Шаг", "plan": None}


@pytest.mark.asyncio
async def test_hint_names_the_job_that_rewind_runs(db):
    now = scheduler.utcnow()
    async with db.session() as s:
        s.add_all(
            [
                Job(employee_id=1, kind="step", ref="d1_access", run_at=now + timedelta(days=2)),
                Job(employee_id=1, kind="step", ref="d1_congrats", run_at=now + timedelta(days=1)),
            ]
        )
        await s.commit()
        emp = await s.get(Employee, 1)
        hint = await demo.show_next(1, s)
        caps = plan_time.captions(emp)
        assert hint == {"title": "Поздравление с первым рабочим днём", "plan": caps["d1_congrats"]}
        result = await demo.run_next(1, s)
        assert result["step"] == "d1_congrats" and result["status"] == "done"
        assert await demo.show_next(1, s) == {
            "title": "Проверка доступов и рабочего места",
            "plan": caps["d1_access"],
        }


@pytest.mark.asyncio
async def test_hint_before_the_bot_is_started_is_the_acquaintance_step(db):
    async with db.session() as s:
        s.add(
            Job(employee_id=1, kind="step", ref="day_before", run_at=scheduler.utcnow() + timedelta(days=1))
        )
        emp = await s.get(Employee, 1)
        emp.status = "invited"
        await s.commit()
        welcome = scenario.step("welcome", "GTP")
        assert await demo.show_next(1, s) == {"title": welcome["title"], "plan": "сразу после запуска по QR"}
        emp.status = "active"
        await s.commit()
        assert (await demo.show_next(1, s))["title"] == scenario.step("day_before", "GTP")["title"]


@pytest.mark.asyncio
async def test_hint_is_empty_when_nothing_is_left_or_employee_is_unknown(db):
    async with db.session() as s:
        assert await demo.show_next(1, s) is None
        assert await demo.show_next(404, s) is None


@pytest.mark.asyncio
async def test_max_receives_the_step_text_without_the_caption(db, monkeypatch):
    client = SimpleNamespace(send=AsyncMock(return_value="mid-1"), edit=AsyncMock())
    monkeypatch.setattr(outbox, "max_client", lambda: client)
    async with db.session() as s:
        emp = await s.get(Employee, 1)
        emp.channel, emp.max_user_id = "max", 777
        await s.commit()
        await scenario.run_step(s, emp, "d1_congrats")
        stored = await s.scalar(select(Message).where(Message.step_id == "d1_congrats"))
        view = next(m for m in await demo.messages(1, s) if m["id"] == stored.id)
    assert await outbox.process_one()
    sent = client.send.await_args_list[0].args[0]
    expected = scenario.render(scenario.step("d1_congrats", "GTP")["messages"][0]["text"], emp)
    assert sent.text == expected and stored.text == expected
    assert "plan" not in sent.to_payload() and "plan" not in stored.payload
    assert view["plan"] == plan_time.captions(emp)["d1_congrats"]  # а в панели подпись есть
