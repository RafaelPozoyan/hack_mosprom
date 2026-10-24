"""Команда адаптации: кто получает уведомления Корсика о новичках.

Рекрутер получает обращения своих новичков, руководитель подразделения — напоминания и рекомендации,
руководитель подбора — еженедельный дайджест. HR-менеджеры работают в панели и в этот список не входят.
Подключение к MAX — по личной ссылке staff_<код> (обработчик в bot/updates.py).
"""

import io
import secrets
from collections import Counter
from typing import Literal

import segno
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.admin import auth, deep_link, staff_view
from app.db import get_session
from app.models import Employee, Staff

ROLES = ("recruiter", "manager", "head")

router = APIRouter(prefix="/api", dependencies=[Depends(auth)])


class MemberIn(BaseModel):
    name: str
    role: Literal["recruiter", "manager", "head"]


class TeamIn(BaseModel):
    manager_id: int | None = None
    recruiter_id: int | None = None


async def _member(s: AsyncSession, staff_id: int) -> Staff:
    x = await s.get(Staff, staff_id)
    if not x or x.role not in ROLES:
        raise HTTPException(404, "Такого человека нет в команде")
    return x


def _name(value: str) -> str:
    name = " ".join(value.split())
    if not name:
        raise HTTPException(422, "Укажите имя и фамилию")
    return name


async def _assigned(s: AsyncSession, staff_id: int) -> None:
    """Новичков нельзя оставить без руководителя или рекрутера молча: сначала их переназначают в карточке."""
    names = list(
        await s.scalars(
            select(Employee.full_name)
            .where(or_(Employee.manager_id == staff_id, Employee.recruiter_id == staff_id))
            .order_by(Employee.full_name)
        )
    )
    if names:
        shown = ", ".join(names[:3]) + (f" и ещё {len(names) - 3}" if len(names) > 3 else "")
        raise HTTPException(409, f"Сначала переназначьте новичков в их карточках: {shown}.")


@router.get("/team")
async def team(s: AsyncSession = Depends(get_session)):
    members = (await s.scalars(select(Staff).where(Staff.role.in_(ROLES)).order_by(Staff.name))).all()
    links = (await s.execute(select(Employee.manager_id, Employee.recruiter_id))).all()
    newcomers = Counter(staff_id for row in links for staff_id in row if staff_id)
    return [{**staff_view(x), "newcomers": newcomers[x.id]} for x in members]


@router.post("/team")
async def add_member(body: MemberIn, s: AsyncSession = Depends(get_session)):
    x = Staff(name=_name(body.name), role=body.role, invite_code=secrets.token_urlsafe(9))
    s.add(x)
    await s.commit()
    return {**staff_view(x), "newcomers": 0}


@router.patch("/team/{staff_id}")
async def edit_member(staff_id: int, body: MemberIn, s: AsyncSession = Depends(get_session)):
    x = await _member(s, staff_id)
    if body.role != x.role:
        await _assigned(s, x.id)  # иначе новичок останется с рекрутером, который рекрутером уже не является
    x.name, x.role = _name(body.name), body.role
    await s.commit()
    return staff_view(x)


@router.delete("/team/{staff_id}")
async def remove_member(staff_id: int, s: AsyncSession = Depends(get_session)):
    x = await _member(s, staff_id)
    await _assigned(s, x.id)
    await s.delete(x)
    await s.commit()
    return {"ok": True}


@router.get("/team/{staff_id}/qr.svg")
async def member_qr(staff_id: int, s: AsyncSession = Depends(get_session)):
    link = deep_link(f"staff_{(await _member(s, staff_id)).invite_code}")
    if not link:
        raise HTTPException(404, "Бот MAX ещё не настроен — ссылка для подключения появится после настройки")
    buf = io.BytesIO()
    segno.make(link, error="m").save(buf, kind="svg", scale=6, dark="#0b3a4a")
    return Response(buf.getvalue(), media_type="image/svg+xml")


@router.put("/employees/{emp_id}/team")
async def assign_team(emp_id: int, body: TeamIn, s: AsyncSession = Depends(get_session)):
    """Сменить руководителя и рекрутера новичка. План адаптации не пересчитывается: от команды он не зависит.
    Новые обращения уходят новому рекрутеру — escalation.finalize берёт его из карточки в момент отправки."""
    e = await s.get(Employee, emp_id)
    if not e:
        raise HTTPException(404, "Новичок не найден")
    for field, role, label in (
        ("manager_id", "manager", "руководителя"),
        ("recruiter_id", "recruiter", "рекрутера"),
    ):
        staff_id = getattr(body, field)
        staff = staff_id and await s.get(Staff, staff_id)
        if staff_id and (not staff or staff.role != role):
            raise HTTPException(422, f"Такого {label} уже нет в команде — выберите другого из списка")
        setattr(e, field, staff_id)
    await s.commit()
    s.expunge(e)  # перечитываем вместе с новыми руководителем и рекрутером
    e = await s.get(Employee, emp_id)
    return {"manager": e.manager and e.manager.name, "recruiter": e.recruiter and e.recruiter.name}
