"""Команда новичка: смена руководителя и рекрутера, обращения уходят новому рекрутеру."""

import asyncio
import os
from contextlib import asynccontextmanager
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app import channels, scheduler
from app.api import admin, team
from app.bot import escalation
from app.db import Base, get_session
from app.models import Employee, Job, Message, Site, Staff, Ticket, TicketDeadline


@pytest_asyncio.fixture
async def db(monkeypatch):
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL нужен для проверки команды на PostgreSQL")
    schema = "team_test_" + uuid4().hex
    owner = create_async_engine(url)
    async with owner.begin() as c:
        await c.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_async_engine(url, connect_args={"server_settings": {"search_path": schema}})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as c:
            tables = [
                Site.__table__,
                Staff.__table__,
                Employee.__table__,
                Ticket.__table__,
                Message.__table__,
                TicketDeadline.__table__,
            ]
            await c.run_sync(lambda conn: Base.metadata.create_all(conn, tables=[*tables, Job.__table__]))
        async with factory() as s:
            s.add(Site(code="GTP", name="Демо"))
            s.add(Staff(id=1, name="Анна Смирнова", role="hr", invite_code="staff-hr"))
            s.add(Staff(id=2, name="Мария Кузнецова", role="recruiter", invite_code="staff-a", max_user_id=7))
            s.add(Staff(id=3, name="Олег Белов", role="recruiter", invite_code="staff-b", max_user_id=8))
            s.add(Staff(id=4, name="Игорь Волков", role="manager", invite_code="staff-m"))
            await s.flush()
            await s.execute(text("SELECT setval('staff_id_seq', 4)"))  # id заданы явно — новые идут следом
            s.add(
                Employee(
                    id=1,
                    full_name="Демо Иван Петров",
                    first_name="Иван",
                    site_code="GTP",
                    recruiter_id=2,
                    manager_id=4,
                    status="active",
                    start_date=date(2026, 10, 26),
                    probation_end=date(2027, 1, 26),
                    invite_code="demo",
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


async def call(db, method: str, path: str, **kw) -> httpx.Response:
    app = FastAPI()
    app.include_router(team.router)
    app.dependency_overrides[admin.auth] = lambda: None

    async def session_override():
        async with db.session() as s:
            yield s

    app.dependency_overrides[get_session] = session_override
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        return await client.request(method, path, **kw)


@pytest.mark.asyncio
async def test_assignment_accepts_only_matching_roles(db):
    wrong = await call(db, "PUT", "/api/employees/1/team", json={"manager_id": 4, "recruiter_id": 4})
    assert wrong.status_code == 422 and "рекрутера" in wrong.json()["detail"]
    assert (await call(db, "PUT", "/api/employees/1/team", json={"recruiter_id": 99})).status_code == 422
    assert (await call(db, "PUT", "/api/employees/9/team", json={})).status_code == 404
    cleared = await call(db, "PUT", "/api/employees/1/team", json={"manager_id": None, "recruiter_id": 2})
    assert cleared.json() == {"manager": None, "recruiter": "Мария Кузнецова"}


@pytest.mark.asyncio
async def test_new_escalation_goes_to_reassigned_recruiter(db, monkeypatch):
    client = SimpleNamespace(send=AsyncMock(return_value="card-mid"))
    monkeypatch.setattr(channels, "max_client", lambda: client)
    monkeypatch.setattr(channels, "settings", lambda: SimpleNamespace(max_hr_chat_id=99))
    card = escalation.Handoff(summary="Демо", suggested_action="Ответить")
    monkeypatch.setattr(escalation, "_handoff", AsyncMock(return_value=card))

    @asynccontextmanager
    async def session():
        async with db.session() as s:
            yield s

    monkeypatch.setattr(scheduler, "session", session)
    monkeypatch.setattr(scheduler, "_job_slots", asyncio.Semaphore(2))
    assert (
        await call(db, "PUT", "/api/employees/1/team", json={"manager_id": 4, "recruiter_id": 3})
    ).status_code == 200
    async with db.session() as s:
        await escalation.create_ticket(s, await s.get(Employee, 1), "question", "Демо: где получить пропуск?")
    assert await scheduler.tick() == 1
    targets = [sent.kwargs for sent in client.send.await_args_list]
    assert targets == [{"chat_id": 99}, {"user_id": 8}]  # групповой чат HR и новый рекрутер, старому — ничего
