"""Команда адаптации: кто получает уведомления Корсика о новичках.

Рекрутер получает обращения своих новичков, руководитель подразделения — напоминания и рекомендации,
руководитель подбора — еженедельный дайджест.
"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.admin import auth
from app.db import get_session
from app.models import Employee, Staff

router = APIRouter(prefix="/api", dependencies=[Depends(auth)])


class TeamIn(BaseModel):
    manager_id: int | None = None
    recruiter_id: int | None = None


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
