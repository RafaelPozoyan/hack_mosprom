"""Входящая очередь вебхука MAX: диалог и намерения доставки фиксируются вместе с событием."""

import asyncio
import hashlib
import json
import logging
from contextlib import AsyncExitStack
from datetime import timedelta

from sqlalchemy import exists, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.bot.updates import EventContext, process_max_update
from app.config import settings
from app.db import session
from app.models import MaxUpdate
from app.scheduler import MAX_ATTEMPTS, utcnow

log = logging.getLogger(__name__)
WORKERS = 2  # медленный ответ одному пользователю не блокирует всех; оставляем соединения API и планировщику
POLL_INTERVAL_S = 1  # очередь проверяется раз в секунду, без активного ожидания


class InvalidUpdate(ValueError):
    pass


def _integer(value) -> int:
    if type(value) is not int or not -(2**63) <= value < 2**63:
        raise InvalidUpdate("Ожидается идентификатор или время MAX")
    return value


def identity(upd: dict) -> tuple[str, str] | None:
    """https://dev.max.ru/docs-api/objects/Update: mid, callback_id или поля bot_started."""
    if not isinstance(upd, dict) or not isinstance(upd.get("update_type"), str):
        raise InvalidUpdate("Некорректное событие MAX")
    kind = upd["update_type"]
    if kind not in {"message_created", "message_callback", "bot_started"}:
        return None
    try:
        if kind == "message_created":
            msg = upd["message"]
            user_id = (msg.get("sender") or {}).get("user_id")
            chat_id = _integer(msg["recipient"]["chat_id"])
            if user_id is not None:
                user_id = _integer(user_id)
            actor = (
                f"chat:{chat_id}"
                if user_id is None or chat_id == settings().max_hr_chat_id
                else f"user:{user_id}"
            )
            # У пересланного сообщения body может отсутствовать: сохраняем идентичность всего события.
            event_id = (msg.get("body") or {}).get("mid") or upd
        elif kind == "message_callback":
            actor = f"user:{_integer(upd['callback']['user']['user_id'])}"
            event_id = upd["callback"]["callback_id"]
            if not isinstance(event_id, str) or not event_id:
                raise InvalidUpdate("callback_id отсутствует")
        else:
            actor = f"user:{_integer(upd['user']['user_id'])}"
            event_id = [_integer(upd["timestamp"]), _integer(upd["chat_id"]), actor, upd.get("payload")]
    except (KeyError, TypeError, AttributeError) as e:
        raise InvalidUpdate("Некорректное событие MAX") from e
    key = hashlib.sha256(json.dumps([kind, event_id], sort_keys=True).encode()).hexdigest()
    return key, actor


async def enqueue(upd: dict) -> None:
    ident = identity(upd)
    if ident is None:
        return
    key, actor = ident
    async with session() as s:
        # Порядок приёма одного пользователя не зависит от времени commit параллельных запросов.
        await s.execute(select(func.pg_advisory_xact_lock(func.hashtextextended(actor, 0))))
        await s.execute(
            insert(MaxUpdate)
            .values(event_key=key, actor=actor, payload=upd)
            .on_conflict_do_nothing(index_elements=[MaxUpdate.event_key])
        )
        await s.commit()  # только после этого webhook может подтвердить получение


async def process_one() -> bool:
    earlier = aliased(MaxUpdate)
    callbacks = []
    # Замок диалога освобождается после внешнего commit, иначе планировщик прочитает старое состояние.
    async with AsyncExitStack() as locks, session() as s, s.begin():
        row = await s.scalar(
            select(MaxUpdate)
            .where(
                MaxUpdate.status == "pending",
                MaxUpdate.available_at <= func.now(),
                ~exists(
                    select(earlier.id).where(
                        earlier.actor == MaxUpdate.actor, earlier.id < MaxUpdate.id, earlier.status != "done"
                    )
                ),
            )
            .order_by(MaxUpdate.id)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        if row is None:
            return False
        # Блокировка живёт до конца обработки. При падении процесса транзакция откатится,
        # событие останется pending; другой воркер не обгоняет его в диалоге этого пользователя.
        row.attempts += 1
        try:
            connection = await s.connection()
            # Commit обработчика только сбрасывает изменения в общую транзакцию.
            # Savepoint позволяет откатить их и сохранить ошибку/паузу самой очереди.
            async with (
                connection.begin_nested(),
                AsyncSession(
                    bind=connection, join_transaction_mode="rollback_only", expire_on_commit=False
                ) as work,
            ):
                work.info["after_commit"] = callbacks
                await process_max_update(row.payload, raise_errors=True, context=EventContext(work, locks))
                await work.flush()
        except Exception as e:
            callbacks.clear()
            row.error = type(
                e
            ).__name__  # не сохраняем сообщения сотрудников или ответы внешнего API в ошибке
            row.status = "failed" if row.attempts >= MAX_ATTEMPTS else "pending"
            row.available_at = utcnow() + timedelta(minutes=row.attempts)  # та же пауза, что у jobs
            log.warning("MAX inbox id=%s status=%s error=%s", row.id, row.status, row.error)
        else:
            row.status, row.done_at, row.error, row.payload = "done", utcnow(), None, {}
    for callback in callbacks:
        try:
            callback()
        except Exception:
            log.exception("MAX inbox post-commit callback failed")
    return True


async def loop() -> None:
    while True:
        try:
            if await process_one():
                continue
        except Exception:
            log.exception("MAX inbox worker failed")
        await asyncio.sleep(POLL_INTERVAL_S)
