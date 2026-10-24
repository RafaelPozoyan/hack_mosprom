"""Обработка событий MAX (webhook или long polling) и демо-канала: привязка пользователя и передача в диалог."""

import asyncio
import logging
import re
from collections import defaultdict

from sqlalchemy import select

from app import channels
from app.bot import dialog, escalation
from app.channels import InEvent, OutMessage
from app.channels.max import max_client
from app.config import settings
from app.db import session
from app.models import Employee, Staff, Ticket

log = logging.getLogger(__name__)

# События одного сотрудника обрабатываются последовательно (двойной клик, быстрые сообщения)
_locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

UNKNOWN_USER = (
    "Привет! Я Корсик 🐾 — ИИ-гид по адаптации новых сотрудников Группы ПОЛИПЛАСТИК.\n"
    "Чтобы начать, откройте меня по QR-коду или ссылке из приглашения рекрутера."
)
CODE_TAKEN = (
    "Этот код приглашения уже привязан к другому аккаунту MAX, или ваш аккаунт уже связан с другим приглашением.\n"
    "Напишите, пожалуйста, своему HR-менеджеру — он поможет."
)
TICKET_REF = re.compile(r"#(\d+)")


async def handle_employee_event(employee_id: int, ev: InEvent) -> None:
    async with _locks[employee_id], session() as s:
        emp = await s.get(Employee, employee_id)
        if emp is None:
            return
        try:
            await dialog.handle(s, emp, ev)
        except Exception:
            log.exception("event handling failed: employee=%s event=%s", employee_id, ev)


def _button_text(message: dict, payload: str) -> str:
    for att in (message.get("body") or {}).get("attachments") or []:
        if att.get("type") == "inline_keyboard":
            for row in att["payload"]["buttons"]:
                for b in row:
                    if b.get("payload") == payload:
                        return b.get("text", "")
    return ""


async def process_max_update(upd: dict) -> None:
    client = max_client()
    kind = upd.get("update_type")
    try:
        if kind == "bot_started":
            await _on_started(upd["user"], upd.get("payload") or "")
        elif kind == "message_callback":
            cb = upd["callback"]
            try:  # снимает «часики» с кнопки; ошибка здесь не должна мешать обработке нажатия
                await client.answer_callback(cb["callback_id"])
            except Exception:
                log.warning("answer_callback failed", exc_info=True)
            emp_id = await _employee_by_user(cb["user"]["user_id"])
            if emp_id:
                msg = upd.get("message") or {}
                await handle_employee_event(
                    emp_id,
                    InEvent("callback", payload=cb.get("payload", ""), text=_button_text(msg, cb.get("payload", "")),
                            callback_id=cb["callback_id"], message_mid=(msg.get("body") or {}).get("mid")),
                )
        elif kind == "message_created":
            msg = upd["message"]
            recipient = msg.get("recipient") or {}
            text = ((msg.get("body") or {}).get("text") or "").strip()
            if settings().max_hr_chat_id and recipient.get("chat_id") == settings().max_hr_chat_id:
                await _on_hr_chat(msg, text)
                return
            user_id = (msg.get("sender") or {}).get("user_id")
            emp_id = await _employee_by_user(user_id)
            if emp_id:
                await handle_employee_event(emp_id, InEvent("text", text=text))
            elif user_id:
                await client.send(OutMessage(text=UNKNOWN_USER), user_id=user_id)
    except Exception:
        log.exception("MAX update failed: %s", upd)


async def _employee_by_user(user_id: int | None) -> int | None:
    if not user_id:
        return None
    async with session() as s:
        return await s.scalar(select(Employee.id).where(Employee.max_user_id == user_id))


async def _on_started(user: dict, payload: str) -> None:
    user_id = user["user_id"]
    async with session() as s:
        if payload.startswith("staff_"):
            staff = await s.scalar(select(Staff).where(Staff.invite_code == payload[6:]))
            if staff and staff.max_user_id not in (None, user_id):
                # иначе по чужой ссылке staff_<код> можно подписаться на дайджесты и эскалации другого сотрудника HR
                await max_client().send(OutMessage(text=CODE_TAKEN), user_id=user_id)
                return
            if staff:
                staff.max_user_id = user_id
                await s.commit()
                await max_client().send(
                    OutMessage(text=f"Готово, {staff.name}! Сюда будут приходить уведомления Корсика об адаптации ваших сотрудников 🐾"),
                    user_id=user_id,
                )
                return
        emp = await s.scalar(select(Employee).where(Employee.invite_code == payload)) if payload else None
        # код из QR привязывается к одному аккаунту: чужой скан (QR на экране демо) не уводит путь новичка
        # на другой телефон, а один аккаунт не становится двумя сотрудниками
        other = emp and await s.scalar(select(Employee.id).where(Employee.max_user_id == user_id, Employee.id != emp.id))
        if emp and (emp.max_user_id not in (None, user_id) or other):
            await max_client().send(OutMessage(text=CODE_TAKEN), user_id=user_id)
            return
        if emp:
            emp.channel, emp.max_user_id = "max", user_id
            await s.commit()
            emp_id = emp.id
        else:
            emp_id = await s.scalar(select(Employee.id).where(Employee.max_user_id == user_id))
    if emp_id:
        await handle_employee_event(emp_id, InEvent("start", payload=payload))
    else:
        await max_client().send(OutMessage(text=UNKNOWN_USER), user_id=user_id)


async def _on_hr_chat(msg: dict, text: str) -> None:
    """Ответ HR реплаем на карточку эскалации или на пересланное уточнение сотрудника → сообщение сотруднику."""
    link = msg.get("link") or {}
    if link.get("type") != "reply" or not text:
        return
    linked, from_bot = link.get("message") or {}, (link.get("sender") or {}).get("is_bot")
    async with session() as s:
        t = await s.scalar(select(Ticket).where(Ticket.hr_chat_mid == linked.get("mid")))
        if not t and from_bot and (m := TICKET_REF.search(linked.get("text") or "")):
            # реплай на уточнение сотрудника («✉️ #12 · …»), а не на карточку: номер обращения есть в тексте
            t = await s.get(Ticket, int(m.group(1)))
        if not t:
            if from_bot:
                await channels.post_hr_chat(OutMessage(text="Не нашёл обращение для этого ответа. Ответьте, пожалуйста, реплаем на карточку обращения."))
            return
        author = (msg.get("sender") or {}).get("first_name") or (msg.get("sender") or {}).get("name") or "HR"
        if text.lower() in ("/close", "/закрыть"):
            await escalation.close(s, t)
        else:
            try:
                await escalation.hr_reply(s, t, text, author)
            except escalation.TicketClosedError as e:
                await channels.post_hr_chat(OutMessage(text=f"Обращение #{t.id}: {e}"))


async def poll_loop() -> None:
    """Long polling — для разработки без публичного HTTPS (в проде MAX рекомендует только webhook)."""
    client = max_client()
    marker = None
    while True:
        try:
            updates, marker = await client.poll(marker)
            for upd in updates:
                asyncio.create_task(process_max_update(upd))
        except Exception:
            log.exception("MAX polling failed")
            await asyncio.sleep(5)
