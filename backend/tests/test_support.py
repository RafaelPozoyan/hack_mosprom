"""«Кому сегодня нужна поддержка» и подключение руководителя: отбор за сутки и приватность на сервере."""

import os
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app import main
from app.analytics import support
from app.api import admin
from app.api import support as support_api
from app.channels import notifications
from app.db import Base, get_session
from app.models import Analysis, Employee, MaxNotification, Message, Site, Staff, SurveyAnswer, Ticket

# Свободный ответ новичка и выбранный им вариант: руководитель не должен увидеть ни то, ни другое
FREE_TEXT = "Руководитель при всех говорит, что у него нет времени на мои вопросы"
LABEL = "Пока ничего не понял"


def answer(emp_id: int, flag: str, hours: float, comment: str | None = None) -> SurveyAnswer:
    return SurveyAnswer(
        employee_id=emp_id,
        survey_id="day3",
        question_id="tasks",
        value={"label": LABEL, "flag": flag},
        comment=comment,
        flagged=True,
        created_at=datetime.now(UTC) - timedelta(hours=hours),
    )


def analysis(emp_id: int, flags: list[str], severity: str, hours: float, source: str = "chat") -> Analysis:
    return Analysis(
        employee_id=emp_id,
        source=source,
        text=FREE_TEXT,
        sentiment="negative",
        emotions=[],
        topics=["руководитель"],
        risk_flags=flags,
        severity=severity,
        summary="",
        created_at=datetime.now(UTC) - timedelta(hours=hours),
    )


@pytest_asyncio.fixture
async def db(monkeypatch):
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL нужен для проверки на PostgreSQL")
    schema = "support_test_" + uuid4().hex
    root = create_async_engine(url)
    async with root.begin() as c:
        await c.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_async_engine(url, connect_args={"server_settings": {"search_path": schema}})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as c:
            await c.run_sync(
                lambda conn: Base.metadata.create_all(
                    conn,
                    tables=[
                        MaxNotification.__table__,
                        Site.__table__,
                        Staff.__table__,
                        Employee.__table__,
                        Ticket.__table__,
                        Message.__table__,
                        SurveyAnswer.__table__,
                        Analysis.__table__,
                    ],
                )
            )

        @asynccontextmanager
        async def session():
            async with factory() as s:
                yield s

        monkeypatch.setattr(notifications, "session", session)
        client = SimpleNamespace(send=AsyncMock(return_value="mid"))
        monkeypatch.setattr(notifications, "max_client", lambda: client)
        async with factory() as s:
            s.add(Site(code="GTP", name="Демо", kind="plant"))
            s.add(
                Staff(id=10, name="Игорь Волков", role="manager", max_user_id=77, invite_code="manager-code")
            )
            await s.flush()
            for i, name in enumerate(
                ["Петров Иван", "Морозова Алина", "Новиков Дмитрий", "Лебедев Павел", "Соколова Ольга"], 1
            ):
                s.add(
                    Employee(
                        id=i,
                        full_name=name,
                        first_name=name.split()[1],
                        site_code="GTP",
                        manager_id=10,
                        start_date=date(2026, 10, 1),
                        probation_end=date(2027, 1, 1),
                        invite_code=f"emp-{i}",
                    )
                )
            await s.commit()

        app = FastAPI()
        app.include_router(admin.router)
        app.include_router(admin.manager_router)
        app.include_router(support_api.router)
        app.dependency_overrides[admin.auth] = lambda: None

        async def session_override():
            async with factory() as s:
                yield s

        app.dependency_overrides[get_session] = session_override
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http:
            yield SimpleNamespace(session=factory, client=client, http=http)
    finally:
        await engine.dispose()
        async with root.begin() as c:
            await c.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await root.dispose()


async def staff_messages(db) -> int:
    async with db.session() as s:
        return await s.scalar(select(func.count()).select_from(Message).where(Message.author == "to_manager"))


@pytest.mark.asyncio
async def test_today_lists_last_day_signals_urgent_first(db):
    assert (await db.http.get("/api/support/today")).json() == []  # пустое состояние блока
    async with db.session() as s:
        s.add_all(
            [
                answer(1, "непонимание задач", 3),
                analysis(1, ["непонимание задач"], "medium", 2, source="survey"),  # комментарий к ответу выше
                analysis(2, ["недостаток информации"], "high", 20),  # срочно по оценке разбора
                answer(3, "трудности с оформлением", 30),  # старше суток — уже не «сегодня»
                answer(4, "перегрузка и выгорание", 1),  # срочно по политике analytics.high, без оценки
                analysis(5, [], "low", 1),  # ответ без тревоги
            ]
        )
        await s.commit()
    r = await db.http.get("/api/support/today")
    assert r.status_code == 200
    people = r.json()
    assert [p["employee_id"] for p in people] == [4, 2, 1]  # срочные первыми, среди них — свежие
    assert people[0]["urgent"] and people[0]["reasons"] == [
        {
            "reason": "перегрузка и выгорание",
            "source": "опрос",
            "at": people[0]["reasons"][0]["at"],
            "urgent": True,
        }
    ]
    assert people[1]["urgent"] and people[1]["reasons"][0]["source"] == "сообщение"
    assert not people[2]["urgent"]
    assert [(x["reason"], x["source"]) for x in people[2]["reasons"]] == [("непонимание задач", "опрос")]
    assert FREE_TEXT not in r.text and LABEL not in r.text  # в блоке причина, а не ответ новичка

    rows = {e["id"]: e for e in (await db.http.get("/api/employees")).json()}
    assert rows[1]["signal"]["reason"] == "непонимание задач"  # сигнал виден напротив человека в списке
    assert rows[3]["signal"]["reason"] == "трудности с оформлением"
    assert rows[4]["signal"]["urgent"]
    assert rows[5]["signal"] is None
    card = (await db.http.get("/api/employees/3/support")).json()
    assert card["today"] is False and card["reasons"][0]["reason"] == "трудности с оформлением"


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["max", "panel", "max_fails"])
async def test_manager_gets_recommendation_without_newcomer_text(db, mode):
    async with db.session() as s:
        if mode == "panel":
            (await s.get(Staff, 10)).max_user_id = None
        s.add_all(
            [
                answer(1, "непонимание задач", 1, comment=FREE_TEXT),
                analysis(1, ["непонимание задач"], "medium", 1, source="survey"),
            ]
        )
        await s.commit()
    if mode == "max_fails":
        db.client.send.side_effect = RuntimeError("MAX недоступен")
    card = (await db.http.get("/api/employees/1/support")).json()
    assert card["refusal"] is None and card["manager"] == "Игорь Волков"
    assert card["manager_in_max"] is (mode != "panel")
    assert card["today"] is True and [x["reason"] for x in card["reasons"]] == ["непонимание задач"]
    for option in card["options"]:
        assert FREE_TEXT not in option["text"] and LABEL not in option["text"] and "{" not in option["text"]
    option = card["options"][0]

    r = await db.http.post("/api/employees/1/support/manager", json={"option": option["id"]})
    assert r.status_code == 200 and r.json()["delivered"] is (False if mode == "panel" else None)
    if mode != "panel":
        assert await notifications.process_one()
        if mode == "max_fails":
            for _ in range(2):
                async with db.session() as s:
                    row = await s.scalar(select(MaxNotification))
                    row.available_at = func.clock_timestamp()
                    await s.commit()
                assert await notifications.process_one()
    if mode == "panel":
        db.client.send.assert_not_awaited()
    else:
        sent = db.client.send.await_args
        assert sent.kwargs["user_id"] == 77
        assert option["text"] in sent.args[0].text
        assert FREE_TEXT not in sent.args[0].text and LABEL not in sent.args[0].text
    # без MAX рекомендация ждёт руководителя на его экране по личной ссылке — тоже без ответов новичка
    screen = await db.http.get("/api/m/manager-code/employees/1")
    assert screen.status_code == 200
    assert FREE_TEXT not in screen.text and LABEL not in screen.text
    note = screen.json()["notifications"][0]
    assert note["text"] == option["text"] and note["request"] == option["title"]
    assert note["delivered"] is (mode == "max")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "private",
    [
        lambda: answer(1, "намерение уйти", 24 * 10),
        # жалоба на руководителя без пометки срочности и старше суток — всё равно повод отказать
        lambda: analysis(
            1, ["непонимание задач", "конфликт с наставником или руководителем"], "low", 24 * 10
        ),
    ],
    ids=["survey", "analysis"],
)
async def test_signal_about_manager_blocks_even_direct_api_call(db, private):
    async with db.session() as s:
        s.add_all([answer(1, "непонимание задач", 1), private()])
        await s.commit()
    card = (await db.http.get("/api/employees/1/support")).json()
    assert card["refusal"] == "Сигнал касается руководителя — обсудите с HR-руководителем"
    r = await db.http.post("/api/employees/1/support/manager", json={"option": card["options"][0]["id"]})
    assert r.status_code == 403
    assert "HR-руководителем" in r.json()["detail"]
    db.client.send.assert_not_awaited()
    assert await staff_messages(db) == 0


@pytest.mark.asyncio
async def test_without_manager_nothing_is_sent(db):
    async with db.session() as s:
        (await s.get(Employee, 1)).manager_id = None
        s.add(answer(1, "непонимание задач", 1))
        await s.commit()
    card = (await db.http.get("/api/employees/1/support")).json()
    assert card["manager"] is None and card["refusal"] == support.NO_MANAGER
    r = await db.http.post("/api/employees/1/support/manager", json={"option": card["options"][0]["id"]})
    assert r.status_code == 409
    assert r.json()["detail"] == support.NO_MANAGER
    db.client.send.assert_not_awaited()
    assert await staff_messages(db) == 0


@pytest.mark.asyncio
async def test_support_api_requires_panel_password(monkeypatch):
    monkeypatch.setattr(admin, "settings", lambda: SimpleNamespace(admin_token="panel-test-secret"))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as api:
        assert (await api.get("/api/support/today")).status_code == 401
        assert (await api.get("/api/employees/1/support")).status_code == 401
        r = await api.post("/api/employees/1/support/manager", json={"option": "one_on_one"})
        assert r.status_code == 401


@pytest.mark.asyncio
async def test_unknown_recommendation_or_employee_is_rejected(db):
    r = await db.http.post("/api/employees/1/support/manager", json={"option": "свой текст руководителю"})
    assert r.status_code == 422
    assert (
        await db.http.post("/api/employees/99/support/manager", json={"option": "one_on_one"})
    ).status_code == 404
    db.client.send.assert_not_awaited()
    assert await staff_messages(db) == 0


@pytest.mark.asyncio
async def test_manager_upcoming_skips_steps_already_sent(db):
    from datetime import UTC, datetime

    from app.bot.scenario import timeline

    async with db.session() as s:
        emp = await s.get(Employee, 1)
        future = [
            st
            for st, run_at in timeline(emp)
            if run_at and st.get("audience", "employee") == "employee" and run_at > datetime.now(UTC)
        ]
        s.add(
            Message(
                employee_id=1,
                direction="out",
                author="bot",
                text="Шаг отправлен досрочно",
                step_id=future[0]["id"],
            )
        )
        await s.commit()
    upcoming = (await db.http.get("/api/m/manager-code/employees/1")).json()["upcoming"]
    assert future[0].get("title", future[0]["id"]) not in [
        u["title"] for u in upcoming
    ]  # досрочно пройденный шаг
    assert upcoming[0]["title"] == future[1].get("title", future[1]["id"])
