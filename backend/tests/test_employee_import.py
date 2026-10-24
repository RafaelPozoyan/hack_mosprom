"""Новички из файла и API кадровой системы: разбор строк, ключ — табельный номер, перенос даты выхода."""

import io
import os
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from openpyxl import Workbook
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api import admin, hr_import
from app.bot import scenario
from app.config import settings
from app.db import Base, get_session
from app.models import Employee, Job, Message, Site, Staff, Ticket

SITES = {
    "GrPP": "Группа ПОЛИПЛАСТИК — офис",
    "Luzhniki": "Офис «Лужники»",
    "GTP": "Производственная площадка GTP",
}
HEADER = [
    "Табельный номер",
    "ФИО",
    "Должность",
    "Подразделение",
    "Площадка",
    "Дата выхода",
    "Руководитель",
    "Рекрутер",
]
TODAY = date(2026, 10, 24)
START = date.today() + timedelta(days=30)  # план строится только из будущих шагов


def xlsx(rows: list[list]) -> bytes:
    wb = Workbook()
    for r in rows:
        wb.active.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_xlsx_with_report_title_and_typed_cells():
    data = xlsx(
        [
            ["Список принятых сотрудников"],
            [],
            HEADER,
            [123, "Иванова  Анна Сергеевна", "Инженер", "ОТК", "GTP", datetime(2026, 11, 2), None, None],
            [None] * 8,
        ]
    )
    rows = hr_import.read_file("Выгрузка.xlsx", data)
    assert [n for n, _ in rows] == [4]
    plan = hr_import.check_rows(rows, SITES, [], {}, TODAY)[0]
    assert plan.status == "new" and plan.personnel_number == "123"
    assert plan.full_name == "Иванова Анна Сергеевна" and plan.start_date == date(2026, 11, 2)


def test_csv_from_1c_in_windows_1251():
    raw = "Таб. №;ФИО;Площадка;Дата приёма\n000123;Петрова Мария;Офис «Лужники»;02.11.2026 0:00:00\n"
    rows = hr_import.read_file("export.csv", raw.encode("cp1251"))
    plan = hr_import.check_rows(rows, SITES, [], {}, TODAY)[0]
    assert plan.status == "new" and plan.personnel_number == "000123"
    assert plan.site_code == "Luzhniki" and plan.start_date == date(2026, 11, 2)


@pytest.mark.parametrize(
    ("name", "data", "problem"),
    [
        ("x.csv", "Табельный номер;ФИО;Площадка\n1;Иванова Анна;GTP\n".encode(), "Дата выхода"),
        ("x.csv", "Имя;Телефон\nАнна;123\n".encode(), "заголовками"),
        ("x.xls", b"old", ".xlsx"),
        ("x.xlsx", b"not a zip", "Excel"),
    ],
)
def test_file_problems_are_explained(name, data, problem):
    with pytest.raises(hr_import.FileProblem, match=problem):
        hr_import.read_file(name, data)


def row(n, number, name, site, start, manager=None, recruiter=None):
    keys = ("personnel_number", "full_name", "site", "start_date", "manager", "recruiter")
    return n, dict(zip(keys, (number, name, site, start, manager, recruiter), strict=True))


def test_row_errors_duplicates_and_staff_warnings():
    staff = [
        SimpleNamespace(id=2, name="Мария Кузнецова", role="recruiter"),
        SimpleNamespace(id=3, name="Игорь Волков", role="manager"),
    ]
    rows = [
        row(
            2, "1", "Иванова Анна", "офис «лужники»", "02.11.2026", "Волков Игорь Петрович", "Кузнецова Мария"
        ),
        row(3, "1", "Иванов Пётр", "GTP", "2026-11-02"),
        row(4, "", "", "Склад", "31.02.2026"),
        row(5, "5", "Орлова Ника", "gtp", "02.11.26 0:00:00", recruiter="Смирнов Олег"),
    ]
    first, duplicate, broken, unknown = hr_import.check_rows(rows, SITES, staff, {}, TODAY)
    assert first.status == "new" and first.site_code == "Luzhniki" and not first.warnings
    assert first.manager.id == 3 and first.recruiter.id == 2
    assert duplicate.status == "error" and "строке 2" in duplicate.errors[0]
    assert broken.status == "error" and len(broken.errors) == 4
    assert "«Склад»" in broken.errors[2] and "«31.02.2026»" in broken.errors[3]
    assert unknown.status == "new" and unknown.recruiter is None and unknown.start_date == date(2026, 11, 2)
    assert "Смирнов Олег" in unknown.warnings[0]


# --- PostgreSQL: запись, повтор и перенос даты ---------------------------------------------------------


@pytest_asyncio.fixture
async def db():
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL нужен для проверки загрузки на PostgreSQL")
    schema = "import_test_" + uuid4().hex
    admin_engine = create_async_engine(url)
    async with admin_engine.begin() as c:
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
            ]
            await c.run_sync(lambda conn: Base.metadata.create_all(conn, tables=[*tables, Job.__table__]))
        async with factory() as s:
            s.add_all(Site(code=code, name=name) for code, name in SITES.items())
            await s.flush()
            s.add(Staff(name="Мария Кузнецова", role="recruiter", invite_code="staff-recruiter"))
            s.add(Staff(name="Игорь Волков", role="manager", invite_code="staff-manager"))
            await s.commit()
        yield SimpleNamespace(session=factory)
    finally:
        await engine.dispose()
        async with admin_engine.begin() as c:
            await c.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await admin_engine.dispose()


async def call(db, method: str, path: str, **kw) -> httpx.Response:
    app = FastAPI()
    app.include_router(hr_import.router)
    app.include_router(hr_import.hr_router)
    app.dependency_overrides[admin.auth] = lambda: None

    async def session_override():
        async with db.session() as s:
            yield s

    app.dependency_overrides[get_session] = session_override
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        return await client.request(method, path, **kw)


async def count(db, model) -> int:
    async with db.session() as s:
        return await s.scalar(select(func.count()).select_from(model))


@pytest.mark.asyncio
async def test_same_file_twice_creates_no_duplicates(db):
    rows = [
        [f"{n:06}", f"Демо{n} Анна", "Оператор", "", "GTP", START, "Игорь Волков", "Мария Кузнецова"]
        for n in range(1, 11)
    ]
    files = {"file": ("Новички.xlsx", xlsx([HEADER, *rows]))}
    preview = (await call(db, "POST", "/api/employees/import/preview", files=files)).json()
    assert preview["counts"] == {"new": 10, "update": 0, "unchanged": 0, "error": 0}
    assert (await call(db, "POST", "/api/employees/import", files=files)).json()["new"] == 10
    jobs = await count(db, Job)
    assert jobs > 0
    again = (await call(db, "POST", "/api/employees/import", files=files)).json()
    assert again == {"new": 0, "update": 0, "unchanged": 10, "error": 0}
    assert await count(db, Employee) == 10 and await count(db, Job) == jobs
    async with db.session() as s:
        emp = await s.scalar(select(Employee).where(Employee.personnel_number == "000001"))
        assert emp.first_name == "Анна" and emp.manager.name == "Игорь Волков" and emp.status == "invited"


@pytest.mark.asyncio
async def test_hr_system_moves_start_date_without_resending_sent_steps(db, monkeypatch):
    monkeypatch.setattr(settings(), "hr_api_key", "test-key")
    headers = {"Authorization": "Bearer test-key"}
    record = {
        "personnel_number": "000777",
        "full_name": "Демо Иван Петрович",
        "site": "GTP",
        "start_date": START.isoformat(),
        "recruiter": "Мария Кузнецова",
    }
    created = (
        await call(db, "POST", "/api/hr-system/employees", json={"employees": [record]}, headers=headers)
    ).json()
    assert created["counts"]["new"] == 1
    emp_id = created["results"][0]["employee_id"]
    jobs_before = await count(db, Job)
    again = (
        await call(db, "POST", "/api/hr-system/employees", json={"employees": [record]}, headers=headers)
    ).json()
    assert again["counts"]["unchanged"] == 1 and await count(db, Job) == jobs_before
    async with db.session() as s:
        # «день до выхода» уже ушёл новичку — перенос даты не должен прислать его ещё раз
        sent = await s.scalar(select(Job).where(Job.employee_id == emp_id, Job.ref == "day_before"))
        sent.status = "done"
        s.add(Message(employee_id=emp_id, direction="out", author="bot", text="Демо", step_id="day_before"))
        await s.commit()
    later = START + timedelta(days=7)
    moved = {"employees": [{**record, "start_date": later.strftime("%d.%m.%Y")}]}
    updated = (await call(db, "POST", "/api/hr-system/employees", json=moved, headers=headers)).json()
    assert updated["counts"]["update"] == 1
    assert updated["results"][0]["changes"][0].startswith("Дата выхода")
    async with db.session() as s:
        emp = await s.get(Employee, emp_id)
        assert emp.start_date == later
        assert emp.probation_end == scenario.add_months(later, settings().probation_months)
        assert emp.recruiter.name == "Мария Кузнецова" and emp.status == "invited"
        jobs = (await s.scalars(select(Job).where(Job.employee_id == emp_id, Job.status == "pending"))).all()
        pending = {j.ref: j.run_at for j in jobs}
        planned = {st["id"]: at for st, at in scenario.timeline(emp)}
        assert "day_before" not in pending
        assert pending["d1_congrats"] == planned["d1_congrats"] and planned["d1_congrats"].date() >= later
        assert len(jobs) == len(pending)
    assert await count(db, Employee) == 1


@pytest.mark.asyncio
async def test_hr_system_api_has_its_own_key(db, monkeypatch):
    body = {"employees": []}
    monkeypatch.setattr(settings(), "hr_api_key", "")
    off = await call(db, "POST", "/api/hr-system/employees", json=body)
    assert off.status_code == 503 and "HR_API_KEY" in off.json()["detail"]
    monkeypatch.setattr(settings(), "hr_api_key", "test-key")
    wrong = await call(
        db, "POST", "/api/hr-system/employees", json=body, headers={"Authorization": "Bearer x"}
    )
    assert wrong.status_code == 401
    ok = await call(
        db, "POST", "/api/hr-system/employees", json=body, headers={"Authorization": "Bearer test-key"}
    )
    assert ok.status_code == 200


@pytest.mark.asyncio
async def test_template_headers_are_recognized(db):
    template = await call(db, "GET", "/api/employees/import/template.xlsx")
    assert template.status_code == 200
    with pytest.raises(hr_import.FileProblem, match="нет строк"):
        hr_import.read_file("Шаблон.xlsx", template.content)
