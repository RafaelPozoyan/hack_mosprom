"""Демо-канал: эмулятор чата MAX в веб-панели. Тот же движок, что и для MAX, — меняется только транспорт.

Позволяет показать продукт без зарегистрированного бота и «перемотать» таймлайн адаптации за минуты.
"""

import asyncio
import json

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.admin import auth
from app.bot import followup
from app.bot.updates import handle_employee_event
from app.channels import InEvent, hub, message_view
from app.db import get_session
from app.models import Job, Message
from app.scheduler import run_job

router = APIRouter(prefix="/api/demo/employees/{emp_id}", dependencies=[Depends(auth)])


class TextIn(BaseModel):
    text: str


class CallbackIn(BaseModel):
    payload: str
    text: str = ""
    message_id: int | None = None


def _bg(emp_id: int, ev: InEvent) -> None:
    asyncio.create_task(handle_employee_event(emp_id, ev))


@router.post("/start")
async def start(emp_id: int):
    _bg(emp_id, InEvent("start"))
    return {"ok": True}


@router.post("/message")
async def message(emp_id: int, body: TextIn):
    _bg(emp_id, InEvent("text", text=body.text))
    return {"ok": True}


@router.post("/callback")
async def callback(emp_id: int, body: CallbackIn):
    _bg(emp_id, InEvent("callback", payload=body.payload, text=body.text,
                        message_mid=f"web:{body.message_id}" if body.message_id else None))
    return {"ok": True}


@router.get("/messages")
async def messages(emp_id: int, s: AsyncSession = Depends(get_session)):
    rows = (await s.scalars(
        select(Message).where(Message.employee_id == emp_id, Message.author.in_(["employee", "bot", "hr"])).order_by(Message.id)
    )).all()
    return [message_view(m) for m in rows]


@router.post("/next")
async def run_next(emp_id: int, s: AsyncSession = Depends(get_session)):
    """Перемотка: выполнить ближайший запланированный шаг таймлайна прямо сейчас."""
    j = await s.scalar(
        select(Job).where(
            Job.employee_id == emp_id,
            Job.status.in_(["pending", "running"]),
            Job.kind.in_(["step", "reminder", *followup.KINDS]),
        ).order_by(Job.run_at, Job.id).limit(1).with_for_update()
    )
    if not j:
        return {"ok": False, "step": None}
    # Сохраняем перемотку до исполнения; после сбоя запрос подберёт планировщик.
    j.status = "running"
    await s.commit()
    await run_job(j.id)
    await s.refresh(j)
    return {"ok": True, "step": j.ref, "run_at": j.run_at.isoformat(), "status": j.status}


@router.get("/stream")
async def stream(emp_id: int):
    q = hub.subscribe(emp_id)

    async def gen():
        try:
            yield ": connected\n\n"
            while True:
                try:
                    ev = await asyncio.wait_for(q.get(), timeout=25)
                    yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
                except TimeoutError:
                    yield ": ping\n\n"
        finally:
            hub.unsubscribe(emp_id, q)

    return StreamingResponse(gen(), media_type="text/event-stream")
