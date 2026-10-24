"""Бесшовная эскалация на живого HR.

Сотрудник остаётся в том же чате с Корсиком: его сообщения уходят HR (в групповой чат MAX и в админку),
ответы HR приходят обратно от имени «HR». ИИ на время диалога молчит — классический human handoff.
HR получает handoff-карточку с контекстом, чтобы сотруднику не пришлось пересказывать проблему.
"""

import logging
from datetime import date, datetime
from zoneinfo import ZoneInfo

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import channels, llm
from app.bot import response_time
from app.bot.scenario import render
from app.channels import ADMIN_FEED, Button, OutMessage, hub
from app.db import after_commit
from app.models import Employee, Job, Message, Ticket

log = logging.getLogger(__name__)

KIND_LABEL = {"question": "вопрос без ответа", "problem": "проблема", "alert": "красный флаг"}


class TicketClosedError(ValueError):
    pass


class Handoff(BaseModel):
    summary: str
    suggested_action: str


def adaptation_day(emp: Employee) -> int:
    return (date.today() - emp.start_date).days + 1


async def _handoff(s: AsyncSession, emp: Employee, subject: str) -> Handoff:
    rows = (
        await s.scalars(
            select(Message)
            .where(Message.employee_id == emp.id, Message.author.in_(["employee", "bot", "hr"]))
            .order_by(Message.id.desc())
            .limit(12)
        )
    ).all()
    history = "\n".join(f"{m.author}: {m.text[:400]}" for m in reversed(rows))
    try:
        return await llm.structured(
            [
                {
                    "role": "system",
                    "content": "Ты помощник HR. Составь короткую handoff-карточку по обращению нового сотрудника: "
                    "summary — суть в 1–2 предложениях без домыслов; suggested_action — что HR стоит сделать первым шагом. "
                    "Опирайся только на переписку.",
                },
                {
                    "role": "user",
                    "content": f"Обращение: {subject}\n\nПереписка (последние сообщения):\n{history}",
                },
            ],
            Handoff,
        )
    except Exception:
        log.exception("handoff summary failed")
        return Handoff(summary=subject[:500], suggested_action="Связаться с сотрудником")


async def queue_ticket(
    s: AsyncSession, emp: Employee, kind: str, subject: str, severity: str = "medium"
) -> Ticket:
    """Добавить обращение, подготовку карточки и срок первого ответа в транзакцию вызывающего обработчика."""
    t = Ticket(
        employee_id=emp.id,
        kind=kind,
        subject=subject,
        severity=severity,
        summary="ИИ готовит handoff-карточку…",
    )
    s.add(t)
    await s.flush()
    # Без employee_id: подготовка резюме не должна удерживать блокировку диалога новичка.
    s.add(Job(kind="handoff", ref=str(t.id), run_at=datetime.now(ZoneInfo("UTC"))))
    response_time.track(s, t, emp)
    return t


async def create_ticket(
    s: AsyncSession, emp: Employee, kind: str, subject: str, severity: str = "medium"
) -> Ticket:
    t = await queue_ticket(s, emp, kind, subject, severity)
    await s.commit()
    after_commit(
        s, hub.publish, ADMIN_FEED, {"type": "ticket", "id": t.id, "kind": kind, "employee": emp.full_name}
    )
    return t


async def finalize(s: AsyncSession, ticket_id: int) -> None:
    """Подготовка карточки из очереди; подтверждённую публикацию в HR-чат не повторяем."""
    t = await s.get(Ticket, ticket_id)
    if t is None or t.status != "open":
        return
    card = await _handoff(s, t.employee, t.subject) if not t.hr_chat_mid else None
    # За время работы LLM обращение могли закрыть. Закрытие и публикация сериализованы по Ticket.
    stmt = (
        select(Ticket)
        .where(Ticket.id == ticket_id)
        .with_for_update(of=Ticket, key_share=True)
        .execution_options(populate_existing=True)
    )
    t = await s.scalar(stmt)
    if t is None or t.status != "open":
        return
    emp = t.employee
    if card and not t.hr_chat_mid:
        t.summary = f"{card.summary}\n\nРекомендуемый шаг: {card.suggested_action}".strip()
    text = (
        f"🆘 Эскалация #{t.id} · {KIND_LABEL.get(t.kind, t.kind)}\n"
        f"👤 {emp.full_name}, {emp.position or '—'} · {emp.site_code} · день {adaptation_day(emp)} адаптации\n"
        f"💬 «{t.subject[:700]}»\n\n🧠 {t.summary}\n\n"
        "Ответьте реплаем на это сообщение — ответ уйдёт сотруднику в чат с Корсиком."
    )
    if not t.hr_chat_mid:
        t.hr_chat_mid = await channels.post_hr_chat(OutMessage(text=text), raise_errors=True)
    await s.commit()  # сохранённый mid позволяет повторить только уведомление рекрутеру
    if emp.recruiter:
        t = await s.scalar(stmt)
        if t is not None and t.status == "open":
            await channels.notify_staff(emp.recruiter, OutMessage(text=text), raise_errors=True)
        await s.commit()
    after_commit(s, hub.publish, ADMIN_FEED, {"type": "ticket_updated", "id": ticket_id})


async def open_dialog(s: AsyncSession, emp: Employee, subject: str, kind: str = "problem") -> Ticket:
    current = await s.get(Ticket, emp.state["ticket_id"]) if emp.state.get("ticket_id") else None
    if current and current.status == "open":
        # Повторное нажатие или новый вопрос при открытом диалоге: второе обращение и второе уведомление не нужны
        emp.state = {k: v for k, v in emp.state.items() if k != "await"}
        await s.commit()
        if subject == current.subject:
            text = "Уже передал — ответ придёт сюда же, в этот чат 🙌"
        else:
            await forward_from_employee(s, emp, subject)
            text = "Добавил это в {твой|ваш} диалог с HR — ответ придёт сюда же, в этот чат 🙌"
        await channels.send(
            s,
            emp,
            OutMessage(
                text=render(text, emp), buttons=[[Button("↩️ Вернуться к Корсику", payload="a:close_dialog")]]
            ),
            ticket_id=current.id,
        )
        return current
    t = await create_ticket(s, emp, kind, subject)
    emp.state = {**{k: v for k, v in emp.state.items() if k != "await"}, "ticket_id": t.id}
    await s.commit()
    await channels.send(
        s,
        emp,
        OutMessage(
            text=render(
                "Передал {твоё|ваше} обращение коллегам из подбора персонала 🙌 Ответ придёт сюда же, в этот чат. "
                "Пока диалог открыт, всё, что {ты напишешь|вы напишете}, я буду пересылать HR.",
                emp,
            ),
            buttons=[[Button("↩️ Вернуться к Корсику", payload="a:close_dialog")]],
        ),
        ticket_id=t.id,
    )
    return t


async def ask_relay(s: AsyncSession, emp: Employee) -> None:
    problem = emp.state.get("last_problem")
    if problem:
        emp.state = {k: v for k, v in emp.state.items() if k != "last_problem"}
        await s.commit()
        await open_dialog(s, emp, problem)
        return
    emp.state = {**emp.state, "await": {"kind": "relay"}}
    await s.commit()
    await channels.send(
        s,
        emp,
        OutMessage(
            text=render(
                "{Опиши|Опишите}, пожалуйста, вопрос или проблему одним сообщением — я передам коллегам из HR.",
                emp,
            ),
            buttons=[[Button("Отмена", payload="a:cancel")]],
        ),
    )


async def forward_from_employee(s: AsyncSession, emp: Employee, text: str) -> None:
    t = await s.get(Ticket, emp.state["ticket_id"])
    if not t or t.status != "open":
        emp.state = {k: v for k, v in emp.state.items() if k != "ticket_id"}
        await s.commit()
        return
    await channels.post_hr_chat(OutMessage(text=f"✉️ #{t.id} · {emp.full_name}:\n{text}"))
    after_commit(s, hub.publish, ADMIN_FEED, {"type": "ticket_message", "id": t.id})


async def hr_reply(s: AsyncSession, t: Ticket, text: str, author: str = "HR") -> None:
    if t.status == "closed":
        raise TicketClosedError("Обращение уже закрыто. Ответ не отправлен.")
    emp = t.employee
    await channels.send(
        s,
        emp,
        OutMessage(
            text=f"💬 {author}:\n{text}",
            buttons=[[Button("↩️ Вернуться к Корсику", payload="a:close_dialog")]],
        ),
        ticket_id=t.id,
        author="hr",
    )
    if emp.state.get("ticket_id") != t.id:
        emp.state = {**emp.state, "ticket_id": t.id}
        await s.commit()


async def close(s: AsyncSession, t: Ticket, notify: bool = True) -> None:
    if t.status == "closed":
        return
    t.status = "closed"
    t.closed_at = datetime.now(ZoneInfo("UTC"))
    emp = t.employee
    if emp.state.get("ticket_id") == t.id:
        emp.state = {k: v for k, v in emp.state.items() if k != "ticket_id"}
    await s.commit()
    if notify:
        await channels.send(
            s,
            emp,
            OutMessage(
                text=render(
                    "Диалог с HR завершён. Если появятся вопросы — {пиши|пишите}, я на связи! 🐾", emp
                )
            ),
        )
    after_commit(s, hub.publish, ADMIN_FEED, {"type": "ticket_closed", "id": t.id})
