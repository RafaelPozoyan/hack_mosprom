"""Доступ по личной ссылке руководителя: изоляция сотрудников и приватность ответов."""

import os
from datetime import date
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api import admin, demo
from app.db import Base, get_session
from app.models import Analysis, Employee, Message, Site, Staff, SurveyAnswer, Ticket, TicketDeadline

SECRET = "manager-test-secret"
PRIVATE = "PRIVATE_NEWCOMER_RESPONSE"
COMPLAINT = "конфликт с наставником или руководителем"


@pytest_asyncio.fixture
async def db(monkeypatch):
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL нужен для проверки на PostgreSQL")
    schema = "manager_access_test_" + uuid4().hex
    root = create_async_engine(url)
    async with root.begin() as connection:
        await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_async_engine(url, connect_args={"server_settings": {"search_path": schema}})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(
                lambda c: Base.metadata.create_all(
                    c,
                    tables=[
                        Site.__table__,
                        Staff.__table__,
                        Employee.__table__,
                        Ticket.__table__,
                        TicketDeadline.__table__,
                        Message.__table__,
                        SurveyAnswer.__table__,
                        Analysis.__table__,
                    ],
                )
            )
        async with factory() as s:
            s.add(Site(code="GTP", name="Тестовая площадка", kind="plant"))
            s.add_all(
                [
                    Staff(id=1, name="Руководитель А", role="manager", invite_code="manager-a"),
                    Staff(id=2, name="Руководитель Б", role="manager", invite_code="manager-b"),
                    Staff(id=3, name="Рекрутер", role="recruiter", invite_code="recruiter-code"),
                ]
            )
            await s.flush()
            for ident in (1, 2):
                s.add(
                    Employee(
                        id=ident,
                        full_name=f"Тестовый сотрудник {ident}",
                        first_name="Тестовый",
                        site_code="GTP",
                        manager_id=ident,
                        recruiter_id=3,
                        start_date=date(2026, 10, 1),
                        probation_end=date(2027, 1, 1),
                        invite_code=f"private-employee-invite-{ident}",
                        state={"survey": {"id": "final", "q": 0, "private_note": PRIVATE}},
                    )
                )
            await s.flush()
            s.add(
                Ticket(id=1, employee_id=1, kind="alert", subject=PRIVATE, severity="high", summary=PRIVATE)
            )
            s.add(
                SurveyAnswer(
                    employee_id=1,
                    survey_id="final",
                    question_id="stay",
                    value={"label": PRIVATE, "flag": COMPLAINT},
                    comment=PRIVATE,
                    flagged=True,
                )
            )
            s.add(
                Analysis(
                    employee_id=1,
                    source="survey",
                    text=PRIVATE,
                    sentiment="negative",
                    emotions=[],
                    topics=[COMPLAINT],
                    risk_flags=[COMPLAINT, "непонимание задач"],
                    severity="high",
                    summary=PRIVATE,
                )
            )
            await s.flush()
            s.add_all(
                [
                    Message(employee_id=1, direction="in", author="employee", text=PRIVATE, ticket_id=1),
                    Message(
                        employee_id=1,
                        direction="out",
                        author="to_manager",
                        text="Проведите встречу с новичком",
                        payload={"delivered": False},
                    ),
                ]
            )
            await s.commit()
        monkeypatch.setattr(
            admin, "settings", lambda: SimpleNamespace(admin_token=SECRET, max_bot_name="manager-test-bot")
        )

        async def deny_external(*args, **kwargs):
            raise AssertionError("External HTTP forbidden in access tests")

        monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", deny_external)
        app = FastAPI()
        app.include_router(admin.router)
        app.include_router(admin.manager_router)
        app.include_router(demo.router)

        async def scoped_session():
            async with factory() as s:
                yield s

        app.dependency_overrides[get_session] = scoped_session
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            yield client
    finally:
        await engine.dispose()
        async with root.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await root.dispose()


@pytest.mark.asyncio
async def test_each_manager_only_lists_own_employee(db):
    for code, own_id in (("manager-a", 1), ("manager-b", 2)):
        result = await db.get(f"/api/m/{code}")
        assert result.status_code == 200
        assert [row["id"] for row in result.json()["employees"]] == [own_id]
        assert PRIVATE not in result.text
        assert "private-employee-invite" not in result.text


@pytest.mark.asyncio
async def test_other_managers_employee_and_unknown_employee_return_404(db):
    for code, own_id, other_id in (("manager-a", 1, 2), ("manager-b", 2, 1)):
        assert (await db.get(f"/api/m/{code}/employees/{own_id}")).status_code == 200
        for ident in (other_id, 999):
            result = await db.get(f"/api/m/{code}/employees/{ident}")
            assert result.status_code == 404
            assert PRIVATE not in result.text


@pytest.mark.asyncio
async def test_unknown_or_non_manager_link_cannot_open_manager_api(db):
    for code in ("missing", "recruiter-code", "private-employee-invite-1"):
        for suffix in ("", "/employees/1"):
            result = await db.get(f"/api/m/{code}{suffix}")
            assert result.status_code == 404
            assert PRIVATE not in result.text


@pytest.mark.asyncio
async def test_private_text_and_complaint_visible_to_hr_but_not_manager(db):
    hr = await db.get("/api/tickets/1", headers={"Authorization": f"Bearer {SECRET}"})
    assert hr.status_code == 200
    assert PRIVATE in hr.text
    manager = await db.get("/api/m/manager-a/employees/1")
    assert manager.status_code == 200
    assert PRIVATE not in manager.text
    assert COMPLAINT not in manager.text
    assert "непонимание задач" not in manager.json()["signals"]
    assert "private-employee-invite" not in manager.text
    assert manager.json()["notifications"][0]["text"] == "Проведите встречу с новичком"


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["anonymous", "manager_bearer", "manager_query"])
async def test_hr_data_and_feed_reject_anonymous_or_manager_credentials(db, mode):
    headers = {"Authorization": "Bearer manager-a"} if mode == "manager_bearer" else {}
    params = {"token": "manager-a"} if mode == "manager_query" else {}
    for path in (
        "/api/employees/1",
        "/api/tickets",
        "/api/tickets/1",
        "/api/demo/employees/1/messages",
        "/api/feed",
        "/api/demo/employees/1/stream",
    ):
        result = await db.get(path, headers=headers, params=params)
        assert result.status_code == 401, (path, result.status_code)
        assert PRIVATE not in result.text
        assert SECRET not in result.text
