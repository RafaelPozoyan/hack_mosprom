"""Устойчивая доставка уведомлений MAX; при неопределённом результате возможен дубль."""

import asyncio
import logging
from datetime import timedelta
from uuid import uuid4

from sqlalchemy import exists, func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.channels.base import Button, OutMessage
from app.channels.max import max_client
from app.db import session
from app.models import MaxNotification, Message, Ticket, TicketDeadline
from app.scheduler import CANDIDATE_BATCH, MAX_ATTEMPTS

log = logging.getLogger(__name__)
LOCK_NAMESPACE = 0x4B4F5255  # KORU: отдельно от jobs и блокировок сотрудника
POLL_INTERVAL_S = 1  # как у остальных очередей: проверка раз в секунду
ACTIVE = ("pending", "running")


async def enqueue(
    s: AsyncSession,
    msg: OutMessage,
    *,
    user_id: int | None = None,
    chat_id: int | None = None,
    dedup_key: str | None = None,
    ticket_id: int | None = None,
    message_id: int | None = None,
    kind: str = "notification",
) -> int:
    """Добавляет намерение в транзакцию вызывающего кода, без внешней отправки или commit."""
    if (user_id is None) == (chat_id is None):
        raise ValueError("Нужен ровно один получатель MAX")
    recipient = f"user:{user_id}" if user_id is not None else f"chat:{chat_id}"
    key = f"{recipient}:{dedup_key or uuid4().hex}"
    # Порядок id соответствует порядку commit даже при двух отправителях одному получателю.
    await s.execute(select(func.pg_advisory_xact_lock(func.hashtextextended(f"notify:{recipient}", 0))))
    ident = await s.scalar(
        insert(MaxNotification)
        .values(
            dedup_key=key,
            user_id=user_id,
            chat_id=chat_id,
            payload={"text": msg.text, **msg.to_payload()},
            kind=kind,
            ticket_id=ticket_id,
            message_id=message_id,
        )
        .on_conflict_do_nothing(index_elements=[MaxNotification.dedup_key])
        .returning(MaxNotification.id)
    )
    return ident or await s.scalar(select(MaxNotification.id).where(MaxNotification.dedup_key == key))


def _ready():
    earlier = aliased(MaxNotification)
    return (
        MaxNotification.status.in_(ACTIVE),
        MaxNotification.available_at <= func.clock_timestamp(),
        ~exists(
            select(earlier.id).where(
                or_(earlier.user_id == MaxNotification.user_id, earlier.chat_id == MaxNotification.chat_id),
                earlier.id < MaxNotification.id,
                earlier.status.in_(ACTIVE),
            )
        ),
    )


async def _confirm(s: AsyncSession, row: MaxNotification, delivered: bool) -> None:
    with s.no_autoflush:
        if row.message_id:
            message = await s.get(Message, row.message_id, with_for_update=True)
            if message:
                message.payload = {**message.payload, "delivered": delivered}
        if row.kind == "hr_card" and delivered and row.ticket_id:
            ticket = await s.scalar(
                select(Ticket).where(Ticket.id == row.ticket_id).with_for_update(of=Ticket)
            )
            if ticket:
                ticket.hr_chat_mid = row.max_mid
        if row.kind == "reminder" and row.ticket_id:
            deadline = await s.get(TicketDeadline, row.ticket_id, with_for_update=True)
            latest = await s.scalar(
                select(func.max(MaxNotification.id)).where(
                    MaxNotification.ticket_id == row.ticket_id,
                    MaxNotification.kind == "reminder",
                )
            )
            if deadline and latest == row.id:
                deadline.reminder_delivered = delivered


async def _deliver(ident: int, client) -> bool:
    async with session() as s, s.begin():
        row = await s.scalar(select(MaxNotification).where(MaxNotification.id == ident, *_ready()))
        if row is None:
            return False
        if row.ticket_id and row.kind in {"hr_card", "handoff", "reminder"}:
            ticket = await s.get(Ticket, row.ticket_id)
            if ticket is None or ticket.status == "closed":
                row.status = "cancelled"
                await _confirm(s, row, False)
                return True
        if row.attempts >= MAX_ATTEMPTS:
            row.status, row.error = "failed", "Последняя попытка прервана до подтверждения"
            await _confirm(s, row, False)
            return True
        row.status, row.attempts = "running", row.attempts + 1
        row.available_at = func.clock_timestamp() + timedelta(minutes=row.attempts)
        payload = row.payload
        target = {"user_id": row.user_id} if row.user_id is not None else {"chat_id": row.chat_id}
    # Попытка уже сохранена. Во время сети нет ни блокировки Ticket, ни транзакции с данными.
    mid, error = None, None
    try:
        mid = await client.send(
            OutMessage(
                text=payload["text"],
                buttons=[[Button(**b) for b in r] for r in payload.get("buttons", [])],
                image=payload.get("image"),
                file=payload.get("file"),
                sources=payload.get("sources", []),
            ),
            **target,
        )
        if not isinstance(mid, str) or not mid:
            raise ValueError("MAX не подтвердил идентификатор сообщения")
    except Exception as exc:
        error = type(exc).__name__
    async with session() as s, s.begin():
        row = await s.get(MaxNotification, ident)
        if row is None:  # связанный сотрудник/обращение могли быть удалены во время сети
            return True
        if error:
            row.status = "failed" if row.attempts >= MAX_ATTEMPTS else "pending"
            row.error = error
            row.available_at = func.clock_timestamp() + timedelta(minutes=row.attempts)
            if row.status == "failed":
                await _confirm(s, row, False)
            log.warning("MAX notification id=%s status=%s error=%s", row.id, row.status, error)
        else:
            row.status, row.error, row.max_mid = "done", None, mid
            await _confirm(s, row, True)
    return True


async def process_one() -> bool:
    client = max_client()
    if client is None:
        return False
    async with session() as guard, guard.begin():
        candidates = (
            await guard.scalars(
                select(MaxNotification.id)
                .where(*_ready())
                .order_by(MaxNotification.id)
                .limit(CANDIDATE_BATCH)
            )
        ).all()
        for ident in candidates:
            if await guard.scalar(
                select(func.pg_try_advisory_xact_lock(LOCK_NAMESPACE, ident))
            ) and await _deliver(ident, client):
                return True
    return False


async def loop() -> None:
    while True:
        try:
            if await process_one():
                continue
        except Exception:
            log.exception("MAX notification worker failed")
        await asyncio.sleep(POLL_INTERVAL_S)
