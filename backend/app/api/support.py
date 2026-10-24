"""API панели HR: «Кому сегодня нужна поддержка» и подключение руководителя из карточки новичка."""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.analytics import support
from app.api.admin import auth
from app.db import get_session
from app.models import Employee

router = APIRouter(prefix="/api", dependencies=[Depends(auth)])


async def _emp(s: AsyncSession, emp_id: int) -> Employee:
    e = await s.get(Employee, emp_id)
    if not e:
        raise HTTPException(404, "Сотрудник не найден")
    return e


@router.get("/support/today")
async def support_today(s: AsyncSession = Depends(get_session)):
    return await support.today(s)


@router.get("/employees/{emp_id}/support")
async def employee_support(emp_id: int, s: AsyncSession = Depends(get_session)):
    return await support.card(s, await _emp(s, emp_id))


class ManagerHelpIn(BaseModel):
    option: str


@router.post("/employees/{emp_id}/support/manager")
async def connect_manager(emp_id: int, body: ManagerHelpIn, s: AsyncSession = Depends(get_session)):
    """Только варианты из сценария: свой текст руководителю через API не отправить."""
    e = await _emp(s, emp_id)
    option = next((o for o in support.options(e) if o["id"] == body.option), None)
    if option is None:
        raise HTTPException(422, "Такой рекомендации нет. Обновите страницу и выберите вариант снова.")
    try:
        m = await support.connect_manager(s, e, option)
    except support.NoManager as exc:
        raise HTTPException(409, str(exc)) from exc
    except support.PrivateSignal as exc:
        raise HTTPException(403, str(exc)) from exc
    return {"delivered": m.payload["delivered"], "manager": e.manager.name}
