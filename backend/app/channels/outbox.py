"""Сохранённые отправки и правки сотрудникам. Неопределённый исход повторяется, дубль возможен."""

import asyncio
import logging
from datetime import timedelta

from sqlalchemy import exists, func, select, update
from sqlalchemy.orm import aliased

from app.channels.base import Button, OutMessage
from app.channels.max import max_client
from app.db import session
from app.models import MaxDelivery, Message
from app.scheduler import MAX_ATTEMPTS, utcnow

log = logging.getLogger(__name__)
WORKERS = 2  # один медленный чат не останавливает всех; оставляем соединения API и другим очередям
POLL_INTERVAL_S = 1  # проверка раз в секунду без активного ожидания


async def process_one() -> bool:
    client = max_client()
    if client is None:
        return False  # отсутствие токена не расходует попытки и не теряет уже поставленные сообщения
    earlier = aliased(MaxDelivery)
    async with session() as s, s.begin():
        row = await s.scalar(
            select(MaxDelivery)
            .join(Message)
            .where(
                MaxDelivery.status.in_(("pending", "running")),
                MaxDelivery.available_at <= func.now(),
                ~exists(
                    select(earlier.message_id).where(
                        earlier.user_id == MaxDelivery.user_id,
                        earlier.message_id < MaxDelivery.message_id,
                        earlier.status != "done",
                    )
                ),
            )
            .order_by(MaxDelivery.message_id)
            .limit(1)
            .with_for_update(of=Message, skip_locked=True)
        )
        if row is None:
            return False
        # После получения замка перечитываем доставку: предыдущий владелец мог
        # завершить отправку или правку между выборкой кандидата и блокировкой.
        await s.refresh(row)
        if row.status not in ("pending", "running") or row.available_at > await s.scalar(select(func.now())):
            return False
        # Замок журнала общий с edit и каскадным удалением сотрудника.
        m = await s.get(Message, row.message_id)
        if row.attempts >= MAX_ATTEMPTS:
            row.status, row.error = "failed", "Последняя попытка прервана до подтверждения"
            return True
        # Счётчик переживает падение во время внешнего вызова. Замок Message остаётся
        # у текущего обработчика, а новая транзакция меняет только запись доставки.
        async with session() as claim, claim.begin():
            await claim.execute(
                update(MaxDelivery)
                .where(MaxDelivery.message_id == row.message_id)
                .values(
                    status="running",
                    attempts=row.attempts + 1,
                    available_at=utcnow() + timedelta(minutes=row.attempts + 1),
                )
            )
        await s.refresh(row)
        try:
            payload = m.payload or {}
            msg = OutMessage(
                text=m.text,
                buttons=[[Button(**b) for b in r] for r in payload.get("buttons", [])],
                image=payload.get("image"),
                file=payload.get("file"),
                sources=payload.get("sources", []),
            )
            if m.max_mid:
                await client.edit(m.max_mid, msg)
            else:
                mid = await client.send(msg, user_id=row.user_id)
                if not isinstance(mid, str) or not mid:
                    raise ValueError("MAX не подтвердил идентификатор сообщения")
                m.max_mid = mid
        except Exception as e:
            row.error = type(e).__name__  # без текстов сотрудников и ответов внешнего API
            row.status = "failed" if row.attempts >= MAX_ATTEMPTS else "pending"
            row.available_at = utcnow() + timedelta(minutes=row.attempts)  # бюджет и паузы как у jobs
            log.warning("MAX outbox message=%s status=%s error=%s", row.message_id, row.status, row.error)
        else:
            row.status, row.error = "done", None
        return True


async def loop() -> None:
    while True:
        try:
            if await process_one():
                continue
        except Exception:
            log.exception("MAX outbox worker failed")
        await asyncio.sleep(POLL_INTERVAL_S)
