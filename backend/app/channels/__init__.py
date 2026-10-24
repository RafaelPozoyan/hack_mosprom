"""Единая точка отправки: каждое сообщение пишется в журнал и уходит в канал сотрудника (MAX или демо-чат)."""

import asyncio
import logging
from collections import defaultdict

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.channels.base import Button, InEvent, OutMessage
from app.channels.max import max_client
from app.config import settings
from app.db import after_commit
from app.models import Employee, MaxDelivery, Message, Staff

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
    after_commit(s, hub.publish, emp.id, {"type": "message", "message": message_view(m)})
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
    recipient = emp.max_user_id if emp.channel == "max" else None
    if recipient:
        # ID журнала следуют порядку commit даже при двух отправителях в один чат.
        await s.execute(
            select(func.pg_advisory_xact_lock(func.hashtextextended(f"outbox:user:{recipient}", 0)))
        )
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
    if recipient:
        s.add(MaxDelivery(message_id=m.id, user_id=recipient))
    await s.commit()
    after_commit(s, hub.publish, emp.id, {"type": "message", "message": message_view(m)})
    return m


async def edit(s: AsyncSession, emp: Employee, m: Message, msg: OutMessage) -> None:
    """Обновляет уже отправленное сообщение (например, отметки в опросе с несколькими ответами)."""
    # Этот же замок удерживает доставщик: правка не потеряется при подтверждении отправки.
    m = await s.scalar(
        select(Message).where(Message.id == m.id).with_for_update().execution_options(populate_existing=True)
    )
    m.text = msg.text
    m.payload = msg.to_payload()
    delivery = await s.get(MaxDelivery, m.id)
    if delivery:
        delivery.status, delivery.attempts, delivery.error = "pending", 0, None
        delivery.available_at = func.now()
    elif emp.channel == "max" and emp.max_user_id and m.max_mid:
        s.add(MaxDelivery(message_id=m.id, user_id=emp.max_user_id))
    await s.commit()
    after_commit(s, hub.publish, emp.id, {"type": "edit", "message": message_view(m)})


async def notify_staff(staff: Staff | None, msg: OutMessage, *, raise_errors: bool = False) -> bool:
    """Личное сообщение руководителю/HR в MAX. Без MAX-аккаунта уведомление видно только в админке."""
    client = max_client()
    if not (staff and staff.max_user_id and client):
        return False
    try:
        await client.send(msg, user_id=staff.max_user_id)
        return True
    except Exception:
        log.exception("notify_staff failed")
        if raise_errors:
            raise
        return False


async def post_hr_chat(msg: OutMessage, *, raise_errors: bool = False) -> str | None:
    """Публикует карточку в групповой чат HR. Ответ HR реплаем на неё уходит сотруднику (см. bot/escalation)."""
    client = max_client()
    chat_id = settings().max_hr_chat_id
    if not (client and chat_id):
        return None
    try:
        return await client.send(msg, chat_id=chat_id)
    except Exception:
        log.exception("post_hr_chat failed")
        if raise_errors:
            raise
        return None


__all__ = [
    "Button",
    "InEvent",
    "OutMessage",
    "edit",
    "hub",
    "log_incoming",
    "notify_staff",
    "post_hr_chat",
    "send",
]
