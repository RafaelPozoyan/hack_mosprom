"""Новички из файла и API кадровой системы: разбор строк, ключ — табельный номер, перенос даты выхода."""

import asyncio
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
    assert plan.status == "new" and plan.personnel_number == "123"  # без ведущих нулей, как после Excel
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
        # табельный хранится без ведущих нулей: Excel всё равно превращает «000001» в число 1
        emp = await s.scalar(select(Employee).where(Employee.personnel_number == "1"))
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


# --- ошибки выгрузки и параллельные загрузки ----------------------------------------------------------


def test_impossible_dates_and_too_long_values_are_row_errors():
    long = {"position": "Д" * 201, "department": "О" * 201}
    rows = [
        row(2, "1", "Иванова Анна", "GTP", "01.01.0001"),  # так 1С выгружает пустую дату
        row(3, "2", "Петров Пётр", "GTP", "31.12.9999"),
        row(4, "3" * 51, "Я" * 201, "GTP", "02.11.2026"),
        (5, {**row(5, "4", "Сидорова Анна", "GTP", "02.11.2026")[1], **long}),
        row(6, "5", "Орлова " + "Н" * 101, "GTP", "02.11.2026"),
    ]
    empty, far, number_and_name, position, first_name = hr_import.check_rows(rows, SITES, [], {}, TODAY)
    assert empty.status == far.status == "error" and empty.start_date is None
    assert "01.01.0001" in empty.errors[0] and "31.12.9999" in far.errors[0]
    assert number_and_name.errors == ["Табельный номер длиннее 50 символов.", "ФИО длиннее 200 символов."]
    assert position.errors == ["Должность длиннее 200 символов.", "Подразделение длиннее 200 символов."]
    assert first_name.errors == ["Имя длиннее 100 символов."]


def test_leading_zeros_do_not_make_another_person():
    rows = [
        row(2, "000501", "Иванова Анна", "GTP", "02.11.2026"),
        row(3, 501.0, "Иванова Анна", "GTP", "02.11.2026"),
    ]
    first, second = hr_import.check_rows(rows, SITES, [], {}, TODAY)
    assert first.personnel_number == "501" and first.status == "new"
    assert second.status == "error" and "строке 2" in second.errors[0]


@pytest.mark.asyncio
async def test_number_without_zeros_finds_saved_newcomer(db, monkeypatch):
    monkeypatch.setattr(settings(), "hr_api_key", "test-key")
    headers = {"Authorization": "Bearer test-key"}
    async with db.session() as s:  # запись до нормализации: номер сохранён с нулями
        s.add(
            Employee(
                full_name="Демо Олег Орлов",
                first_name="Олег",
                site_code="GTP",
                start_date=START,
                probation_end=START + timedelta(days=90),
                invite_code="old",
                personnel_number="000777",
            )
        )
        await s.commit()
    batch = [
        {
            "personnel_number": "000501",
            "full_name": "Демо Анна Иванова",
            "site": "GTP",
            "start_date": START.isoformat(),
        },
        {
            "personnel_number": 777,
            "full_name": "Демо Олег Орлов",
            "site": "GTP",
            "start_date": START.isoformat(),
        },
    ]
    first = (
        await call(db, "POST", "/api/hr-system/employees", json={"employees": batch}, headers=headers)
    ).json()
    assert [r["status"] for r in first["results"]] == ["new", "unchanged"]
    batch[0]["personnel_number"] = 501  # та же строка после Excel: ведущие нули пропали
    again = (
        await call(db, "POST", "/api/hr-system/employees", json={"employees": batch}, headers=headers)
    ).json()
    assert again["counts"]["unchanged"] == 2 and await count(db, Employee) == 2
    async with db.session() as s:
        assert await s.scalar(select(Employee.id).where(Employee.personnel_number == "501"))


@pytest.mark.asyncio
async def test_bad_date_and_empty_optional_fields_do_not_break_batch(db, monkeypatch):
    monkeypatch.setattr(settings(), "hr_api_key", "test-key")
    headers = {"Authorization": "Bearer test-key"}
    batch = [
        {
            "personnel_number": "1",
            "full_name": "Демо Анна Иванова",
            "site": "GTP",
            "start_date": START.isoformat(),
            "position": None,
            "department": None,
            "manager": None,
            "recruiter": None,
        },
        {"personnel_number": "2", "full_name": "Демо Пётр Петров", "site": "GTP", "start_date": "01.01.0001"},
    ]
    response = await call(db, "POST", "/api/hr-system/employees", json={"employees": batch}, headers=headers)
    assert response.status_code == 200
    assert response.json()["counts"] == {"new": 1, "update": 0, "unchanged": 0, "error": 1}
    assert await count(db, Employee) == 1


@pytest.mark.asyncio
async def test_newcomer_is_saved_only_together_with_plan(db, monkeypatch):
    monkeypatch.setattr(settings(), "hr_api_key", "test-key")

    async def broken(s, emp):
        raise RuntimeError("план не построился")

    monkeypatch.setattr(hr_import, "schedule_employee", broken)
    record = {
        "personnel_number": "1",
        "full_name": "Демо Анна Иванова",
        "site": "GTP",
        "start_date": START.isoformat(),
    }
    with pytest.raises(RuntimeError):
        await call(
            db,
            "POST",
            "/api/hr-system/employees",
            json={"employees": [record]},
            headers={"Authorization": "Bearer test-key"},
        )
    assert (
        await count(db, Employee) == 0
    )  # повторная загрузка создаст новичка заново, а не скажет «без изменений»


@pytest.mark.asyncio
async def test_parallel_batches_replan_newcomer_one_after_another(db, monkeypatch):
    monkeypatch.setattr(settings(), "hr_api_key", "test-key")
    headers = {"Authorization": "Bearer test-key"}
    record = {
        "personnel_number": "1",
        "full_name": "Демо Анна Иванова",
        "site": "GTP",
        "start_date": START.isoformat(),
    }
    await call(db, "POST", "/api/hr-system/employees", json={"employees": [record]}, headers=headers)
    real, order = hr_import.schedule_employee, []

    async def tracked(s, emp):
        order.append("start")
        await asyncio.sleep(
            0.3
        )  # второй пакет успел бы начать своё перепланирование, если бы его ничто не держало
        await real(s, emp)
        order.append("end")

    monkeypatch.setattr(hr_import, "schedule_employee", tracked)
    real_apply, arrived, both = hr_import.apply, [], asyncio.Event()

    async def apply_together(s, plans):  # оба пакета успели проверить записи до того, как любой начал запись
        arrived.append(plans)
        if len(arrived) == 2:
            both.set()
        await asyncio.wait_for(both.wait(), 5)
        await real_apply(s, plans)

    monkeypatch.setattr(hr_import, "apply", apply_together)
    later = START + timedelta(days=7)
    moved = {"employees": [{**record, "start_date": later.isoformat()}]}
    replies = await asyncio.gather(
        *(call(db, "POST", "/api/hr-system/employees", json=moved, headers=headers) for _ in range(2))
    )
    assert [r.status_code for r in replies] == [200, 200]
    assert order == ["start", "end"]  # второй пакет дождался первого и увидел, что менять уже нечего
    async with db.session() as s:
        emp = await s.scalar(select(Employee))
        refs = list(
            await s.scalars(select(Job.ref).where(Job.employee_id == emp.id, Job.status == "pending"))
        )
        assert emp.start_date == later and refs and len(refs) == len(set(refs))
