"""Единая точка отправки: каждое сообщение пишется в журнал и уходит в канал сотрудника (MAX или демо-чат)."""

import asyncio
import logging
from collections import defaultdict

from sqlalchemy.ext.asyncio import AsyncSession

from app.channels.base import Button, InEvent, OutMessage
from app.channels.max import max_client
from app.config import settings
from app.models import Employee, Message, Staff

log = logging.getLogger(__name__)

ADMIN_FEED = 0  # ключ подписки админки на общие события (новые эскалации)


class Hub:
    """Pub/sub в памяти процесса для SSE: демо-чат и живая лента админки."""

    def __init__(self):
        self.subs: dict[int, set[asyncio.Queue]] = defaultdict(set)

    def subscribe(self, key: int) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self.subs[key].add(q)
        return q

    def unsubscribe(self, key: int, q: asyncio.Queue) -> None:
        self.subs[key].discard(q)

    def publish(self, key: int, event: dict) -> None:
        for q in self.subs.get(key, ()):
            q.put_nowait(event)


hub = Hub()


def message_view(m: Message) -> dict:
    return {
        "id": m.id,
        "direction": m.direction,
        "author": m.author,
        "text": m.text,
        "payload": m.payload or {},
        "step_id": m.step_id,
        "ticket_id": m.ticket_id,
        "created_at": m.created_at.isoformat() if m.created_at else None,
    }


async def log_incoming(s: AsyncSession, emp: Employee, text: str, payload: dict | None = None) -> Message:
    m = Message(employee_id=emp.id, direction="in", author="employee", text=text, payload=payload or {})
    s.add(m)
    await s.commit()
    hub.publish(emp.id, {"type": "message", "message": message_view(m)})
    return m


async def send(
    s: AsyncSession,
    emp: Employee,
    msg: OutMessage,
    *,
    step_id: str | None = None,
    ticket_id: int | None = None,
    author: str = "bot",
) -> Message:
    m = Message(
        employee_id=emp.id,
        direction="out",
        author=author,
        text=msg.text,
        payload=msg.to_payload(),
        step_id=step_id,
        ticket_id=ticket_id,
    )
    s.add(m)
    await s.flush()
    client = max_client()
    if emp.channel == "max" and emp.max_user_id and client:
        m.max_mid = await client.send(msg, user_id=emp.max_user_id)
    await s.commit()
    hub.publish(emp.id, {"type": "message", "message": message_view(m)})
    return m


async def edit(s: AsyncSession, emp: Employee, m: Message, msg: OutMessage) -> None:
    """Обновляет уже отправленное сообщение (например, отметки в опросе с несколькими ответами)."""
    m.text = msg.text
    m.payload = msg.to_payload()
    client = max_client()
    if emp.channel == "max" and m.max_mid and client:
        await client.edit(m.max_mid, msg)
    await s.commit()
    hub.publish(emp.id, {"type": "edit", "message": message_view(m)})


async def notify_staff(staff: Staff | None, msg: OutMessage) -> bool:
    """Личное сообщение руководителю/HR в MAX. Без MAX-аккаунта уведомление видно только в админке."""
    client = max_client()
    if not (staff and staff.max_user_id and client):
        return False
    try:
        await client.send(msg, user_id=staff.max_user_id)
        return True
    except Exception:
        log.exception("notify_staff failed")
        return False


async def post_hr_chat(msg: OutMessage) -> str | None:
    """Публикует карточку в групповой чат HR. Ответ HR реплаем на неё уходит сотруднику (см. bot/escalation)."""
    client = max_client()
    chat_id = settings().max_hr_chat_id
    if not (client and chat_id):
        return None
    try:
        return await client.send(msg, chat_id=chat_id)
    except Exception:
        log.exception("post_hr_chat failed")
        return None


__all__ = ["Button", "InEvent", "OutMessage", "edit", "hub", "log_incoming", "notify_staff", "post_hr_chat", "send"]
