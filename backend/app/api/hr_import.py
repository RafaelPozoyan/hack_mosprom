"""Новички из кадровой системы: пакетный API для 1С и похожих систем.

Ключ записи — табельный номер: повтор того же пакета не создаёт дублей, а перенос даты выхода перепланирует
ещё не отправленные шаги тем же schedule_employee, что и правка сотрудника в панели (PATCH /api/employees).
"""

import re
import secrets
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.admin import deep_link
from app.bot.scenario import add_months, schedule_employee
from app.config import settings
from app.db import get_session
from app.models import Employee, Site, Staff

# Настройка команды: столько записей ещё можно проверить глазами, а выгрузка всего штата вместо списка
# новичков упрётся в лимит, а не разошлёт сотни приглашений.
MAX_ROWS = 500

# Поля записи и их названия для HR: заголовки колонок файла и подписи изменений.
COLUMNS = {
    "personnel_number": "Табельный номер",
    "full_name": "ФИО",
    "position": "Должность",
    "department": "Подразделение",
    "site": "Площадка",
    "start_date": "Дата выхода",
    "manager": "Руководитель",
    "recruiter": "Рекрутер",
}
STATUSES = ("new", "update", "unchanged", "error")
FIELD_COLUMN = {
    "site_code": "site",
    "manager_id": "manager",
    "recruiter_id": "recruiter",
}  # для подписи изменений


def _key(value) -> str:
    return " ".join(re.sub(r"[^\w№]+", " ", str(value or "").lower().replace("ё", "е")).split())


def _text(value) -> str:
    return " ".join(str(value).split()) if value is not None else ""


def _date(value) -> date | None:
    if isinstance(value, date):
        return value
    head = _text(value).split(" ")[0]  # 1С выгружает дату со временем: «02.11.2026 0:00:00»
    for fmt in ("%d.%m.%Y", "%d.%m.%y", "%Y-%m-%d"):
        try:
            return datetime.strptime(head, fmt).date()
        except ValueError:
            continue
    return None


# --- проверка записей --------------------------------------------------------------------------------


@dataclass
class Plan:
    row: int
    personnel_number: str
    full_name: str
    position: str = ""
    department: str = ""
    site_code: str | None = None
    start_date: date | None = None
    manager: Staff | None = None
    recruiter: Staff | None = None
    status: str = "new"  # new | update | unchanged | error
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    changes: list[str] = field(default_factory=list)
    employee: Employee | None = None  # новичок с этим табельным номером, если он уже загружен


def _same_person(a: str, b: str) -> bool:
    """«Кузнецова Мария Ивановна» из 1С — та же, что «Мария Кузнецова» в панели: порядок и отчество не важны."""
    x, y = set(_key(a).split()), set(_key(b).split())
    return min(len(x), len(y)) >= 2 and (x <= y or y <= x)


def _staff(p: Plan, raw, staff: list, role: str, label: str) -> Staff | None:
    name = _text(raw)
    if not name:
        return None
    found = [x for x in staff if x.role == role and _same_person(name, x.name)]
    if len(found) == 1:
        return found[0]
    problem = "в панели Корсика несколько человек с таким именем" if found else "нет в панели Корсика"
    p.warnings.append(f"{label} «{name}»: {problem} — поле не изменится.")
    return None


def _fields(p: Plan) -> dict:
    """Поля сотрудника из записи. Пустое значение не стирает то, что уже заполнено в панели."""
    values = {
        "full_name": p.full_name,
        "position": p.position,
        "department": p.department,
        "site_code": p.site_code,
        "start_date": p.start_date,
        "manager_id": p.manager and p.manager.id,
        "recruiter_id": p.recruiter and p.recruiter.id,
    }
    return {k: v for k, v in values.items() if v}


def _updates(e: Employee, p: Plan) -> dict:
    return {k: v for k, v in _fields(p).items() if v != getattr(e, k)}


def check_rows(rows: list[tuple[int, dict]], sites: dict[str, str], staff: list, existing: dict, today: date):
    """Проверка без записи: что будет с каждой записью и почему она не подходит — простыми словами."""
    site_by_name = {_key(name): code for code, title in sites.items() for name in (code, title)}
    staff_names = {x.id: x.name for x in staff}
    seen: dict[str, int] = {}
    plans = []
    for n, raw in rows:
        p = Plan(n, _text(raw.get("personnel_number")), _text(raw.get("full_name")))
        p.position, p.department = _text(raw.get("position")), _text(raw.get("department"))
        p.employee = existing.get(p.personnel_number)
        if not p.personnel_number:
            p.errors.append("Нет табельного номера.")
        elif p.personnel_number in seen:
            p.errors.append(
                f"Табельный номер {p.personnel_number} уже есть в строке {seen[p.personnel_number]}."
            )
        else:
            seen[p.personnel_number] = n
        if not p.full_name:
            p.errors.append("Нет ФИО.")
        site = _text(raw.get("site"))
        p.site_code = site_by_name.get(_key(site))
        if not p.site_code:
            known = "; ".join(f"{title} ({code})" for code, title in sites.items())
            p.errors.append(f"Площадка «{site}» не найдена. Подходят: {known}." if site else "Нет площадки.")
        p.start_date = _date(raw.get("start_date"))
        value = _text(raw.get("start_date"))
        if not p.start_date:
            p.errors.append(
                f"«{value}» не похоже на дату выхода — нужна дата вида 02.11.2026."
                if value
                else "Нет даты выхода."
            )
        elif add_months(p.start_date, settings().probation_months) < today:
            p.warnings.append("Испытательный срок по этой дате уже закончился — сообщений по плану не будет.")
        p.manager = _staff(p, raw.get("manager"), staff, "manager", "Руководитель")
        p.recruiter = _staff(p, raw.get("recruiter"), staff, "recruiter", "Рекрутер")
        if p.errors:
            p.status = "error"
        elif p.employee:
            changes = _updates(p.employee, p)
            p.status = "update" if changes else "unchanged"
            for k, new_value in changes.items():
                names = sites if k == "site_code" else staff_names if k.endswith("_id") else {}
                old, new = (
                    x.strftime("%d.%m.%Y") if isinstance(x, date) else names.get(x, x)
                    for x in (getattr(p.employee, k), new_value)
                )
                p.changes.append(f"{COLUMNS[FIELD_COLUMN.get(k, k)]}: {old or '—'} → {new}")
        plans.append(p)
    return plans


async def plan_rows(s: AsyncSession, rows: list[tuple[int, dict]]) -> list[Plan]:
    numbers = {_text(raw.get("personnel_number")) for _, raw in rows} - {""}
    existing = (await s.scalars(select(Employee).where(Employee.personnel_number.in_(numbers)))).all()
    sites = {x.code: x.name for x in (await s.scalars(select(Site).order_by(Site.code))).all()}
    staff = list((await s.scalars(select(Staff))).all())
    return check_rows(rows, sites, staff, {e.personnel_number: e for e in existing}, date.today())


def _first_name(full_name: str) -> str:
    parts = full_name.split()
    return parts[1 if len(parts) > 1 else 0]  # как в create_employee: «Фамилия Имя Отчество»


def counts(plans: list[Plan]) -> dict[str, int]:
    found = Counter(p.status for p in plans)
    return {k: found[k] for k in STATUSES}


def plan_view(p: Plan) -> dict:
    """Что будет (или стало) с записью и почему — для ответа API и предпросмотра в панели."""
    return {
        "row": p.row,
        "personnel_number": p.personnel_number,
        "full_name": p.full_name,
        "position": p.position,
        "site_code": p.site_code,
        "start_date": p.start_date and p.start_date.isoformat(),
        "status": p.status,
        "employee_id": p.employee and p.employee.id,
        "invite_link": p.employee and deep_link(p.employee.invite_code),
        "errors": p.errors,
        "warnings": p.warnings,
        "changes": p.changes,
    }


async def apply(s: AsyncSession, plans: list[Plan]) -> None:
    """Записать новые и изменённые записи. План пересчитывается, только если сдвинулись дата или площадка."""
    months = settings().probation_months
    for p in plans:
        if p.status == "new":
            p.employee = Employee(
                **_fields(p),
                personnel_number=p.personnel_number,
                first_name=_first_name(p.full_name),
                probation_end=add_months(p.start_date, months),
                invite_code=secrets.token_urlsafe(9),
                state={},
            )
            s.add(p.employee)
            replan = True
        elif p.status == "update":
            changes = _updates(p.employee, p)
            for k, v in changes.items():
                setattr(p.employee, k, v)
            if "full_name" in changes:
                p.employee.first_name = _first_name(p.full_name)
            if "start_date" in changes:
                p.employee.probation_end = add_months(p.start_date, months)
            replan = "start_date" in changes or "site_code" in changes
        else:
            continue
        try:
            await s.commit()
        except IntegrityError as e:  # тот же табельный номер успел записать параллельный запрос
            await s.rollback()
            raise HTTPException(
                409, "Эти новички уже загружаются. Обновите список и повторите загрузку."
            ) from e
        if replan:
            await schedule_employee(s, p.employee)


# --- API кадровой системы ----------------------------------------------------------------------------


def hr_key(authorization: str | None = Header(None)) -> None:
    key = settings().hr_api_key
    if not key:
        raise HTTPException(
            503, "API кадровой системы выключен: задайте HR_API_KEY в .env и перезапустите Корсика"
        )
    if not secrets.compare_digest((authorization or "").encode(), f"Bearer {key}".encode()):
        raise HTTPException(401, "Неверный ключ доступа")


hr_router = APIRouter(prefix="/api/hr-system", dependencies=[Depends(hr_key)])


class HrEmployee(BaseModel):
    personnel_number: str | int
    full_name: str
    position: str = ""
    department: str = ""
    site: str  # код или название площадки
    start_date: date | str  # 2026-11-02 или 02.11.2026
    manager: str = ""  # ФИО, как в панели Корсика
    recruiter: str = ""


class HrBatch(BaseModel):
    employees: list[HrEmployee]


@hr_router.post("/employees")
async def upsert_employees(body: HrBatch, s: AsyncSession = Depends(get_session)):
    """Создать или обновить новичков по табельному номеру. Повтор того же пакета ничего не меняет.
    status в ответе: new — создан, update — обновлён, unchanged — без изменений, error — не записан (см. errors)."""
    if len(body.employees) > MAX_ROWS:
        raise HTTPException(413, f"За один запрос — не больше {MAX_ROWS} сотрудников")
    plans = await plan_rows(s, [(i, x.model_dump()) for i, x in enumerate(body.employees, start=1)])
    await apply(s, plans)
    return {"counts": counts(plans), "results": [plan_view(p) for p in plans]}
