"""Срок первого ответа HR на обращение новичка.

За ответ отвечает рекрутер новичка (заказчик, чек-поинт 24.10). Срок считается в рабочих часах площадки
по календарю сценария, поэтому вопрос ночью или в выходной ждёт утра. Срок вышел — напоминание рекрутеру
и уведомление HR-руководителю; срочное обращение HR-руководитель получает сразу. Ответ HR — его сообщение
в обращении (escalation.hr_reply): время первого ответа берётся из журнала, отдельной отметки нет.
"""

from collections.abc import Iterator, Sequence
from datetime import UTC, datetime, time, timedelta
from statistics import median
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import channels
from app.bot.scenario import is_workday, scenario
from app.channels import OutMessage
from app.config import settings
from app.models import Employee, Job, Message, Staff, Ticket, TicketDeadline

KIND = "response_due"


class ReminderError(ValueError):
    pass


def rules() -> dict:
    return scenario()["response_time"]


def _periods(start: datetime, tz: ZoneInfo) -> Iterator[tuple[datetime, datetime]]:
    """Рабочие интервалы площадки начиная с start, без конца: будни без праздников РФ, часы из сценария."""
    cfg = rules()
    day = start.astimezone(tz).date()
    while True:
        if is_workday(day):
            begin = datetime.combine(day, time.fromisoformat(cfg["day_start"]), tzinfo=tz)
            end = datetime.combine(day, time.fromisoformat(cfg["day_end"][day.weekday()]), tzinfo=tz)
            if end > start:
                yield max(begin, start), end
        day += timedelta(days=1)


def deadline(created: datetime, severity: str, tz: ZoneInfo) -> datetime:
    """Момент, когда истекают рабочие часы срока; срочному обращению — свой, более короткий срок."""
    left = timedelta(hours=rules()["urgent_hours" if severity == "high" else "hours"])
    for begin, end in _periods(created, tz):
        if end - begin >= left:
            return begin + left
        left -= end - begin


def work_time(start: datetime, end: datetime, tz: ZoneInfo) -> timedelta:
    """Рабочее время площадки между двумя моментами — так считается время первого ответа HR."""
    total = timedelta()
    for begin, finish in _periods(start, tz):
        if begin >= end:
            return total
        total += min(finish, end) - begin


def _tz(t: Ticket) -> ZoneInfo:
    return ZoneInfo(t.employee.site.tz)


def track(s: AsyncSession, t: Ticket, emp: Employee) -> None:
    """Срок и его проверки сохраняются в транзакции обращения, как и подготовка карточки HR."""
    now = datetime.now(UTC)
    due = deadline(now, t.severity, ZoneInfo(emp.site.tz))
    s.add(TicketDeadline(ticket_id=t.id, due_at=due))
    # Без employee_id, как карточка HR: проверка срока не удерживает диалог новичка.
    if t.severity == "high":
        s.add(Job(kind=KIND, ref=str(t.id), run_at=now))  # срочное — HR-руководителю сразу
    s.add(Job(kind=KIND, ref=str(t.id), run_at=due))


async def _replied(s: AsyncSession, ticket_id: int) -> bool:
    stmt = select(Message.id).where(Message.ticket_id == ticket_id, Message.author == "hr").limit(1)
    return await s.scalar(stmt) is not None


def _card(t: Ticket, title: str, note: str = "") -> OutMessage:
    emp = t.employee
    recruiter = emp.recruiter.name if emp.recruiter else "не назначен"
    return OutMessage(
        text=f"{title}\n👤 {emp.full_name} · {emp.site_code}\n💬 «{t.subject[:300]}»\n"
        f"Ответственный рекрутер: {recruiter}{note}\n"
        f"Обращение в панели: {settings().public_base_url}/tickets?id={t.id}"
    )


def _reminder(t: Ticket) -> OutMessage:
    return _card(t, f"⏰ Новичок ждёт ответа HR: обращение #{t.id}")


async def _notify_heads(s: AsyncSession, t: Ticket, msg: OutMessage, event: str) -> None:
    # HR-руководитель, который сам ведёт новичка, уже получил карточку или напоминание как рекрутер.
    heads = select(Staff).where(Staff.role == "head", Staff.id != t.employee.recruiter_id)
    for head in (await s.scalars(heads)).all():
        await channels.notify_staff(s, head, msg, ticket_id=t.id, kind="handoff", dedup_key=f"{event}:{t.id}")


async def check(s: AsyncSession, ticket_id: int) -> None:
    """Задача очереди: срочное — HR-руководителю сразу, после срока — напоминание рекрутеру и HR-руководителю.

    Отметки и намерения доставки фиксируются одной транзакцией. Доставщик отдельно подтверждает
    напоминание рекрутеру; до подтверждения панель показывает ожидание."""
    d = await s.scalar(select(TicketDeadline).where(TicketDeadline.ticket_id == ticket_id).with_for_update())
    t = await s.get(Ticket, ticket_id)
    if d is None or t is None or t.status != "open" or await _replied(s, ticket_id):
        return
    now = datetime.now(UTC)
    urgent = t.severity == "high" and d.urgent_at is None
    overdue = d.due_at <= now and d.overdue_at is None
    # Без рекрутера напоминать некому; кнопка в панели могла напомнить раньше задачи.
    remind = overdue and t.employee.recruiter is not None and d.reminded_at is None
    if urgent:
        d.urgent_at = now
    if overdue:
        d.overdue_at = now
    if remind:
        d.reminded_at = now
    if urgent:
        await _notify_heads(s, t, _card(t, f"🚨 Срочное обращение #{t.id} — красный флаг"), "urgent")
    if remind:
        queued = await channels.notify_staff(
            s,
            t.employee.recruiter,
            _reminder(t),
            ticket_id=t.id,
            kind="reminder",
            dedup_key=f"reminder:{t.id}:{d.reminded_at.isoformat()}",
        )
        d.reminder_delivered = None if queued is not None else False
    if overdue:
        delivered = (
            "ожидает доставки в MAX"
            if d.reminder_delivered is None
            else "доставлено в MAX"
            if d.reminder_delivered
            else "в MAX не дошло — свяжитесь лично"
        )
        note = f" (напоминание {delivered})" if t.employee.recruiter else ""
        await _notify_heads(
            s, t, _card(t, f"⏰ Обращение #{t.id} ждёт ответа HR дольше срока", note), "overdue"
        )
    await s.commit()


async def remind(s: AsyncSession, t: Ticket) -> bool | None:
    """Кнопка «Напомнить рекрутеру»: напоминание ставится в очередь; до подтверждения возвращается None."""
    if t.status != "open" or await _replied(s, t.id):
        raise ReminderError("HR уже ответил или обращение закрыто — напоминать не нужно.")
    if t.employee.recruiter is None:
        raise ReminderError("У новичка не назначен рекрутер — напомнить некому.")
    d = await s.get(TicketDeadline, t.id, with_for_update=True)
    if d is None:  # обращение создано до появления сроков
        d = TicketDeadline(ticket_id=t.id, due_at=deadline(t.created_at, t.severity, _tz(t)))
        s.add(d)
    d.reminded_at = datetime.now(UTC)
    await s.flush()
    queued = await channels.notify_staff(
        s,
        t.employee.recruiter,
        _reminder(t),
        ticket_id=t.id,
        kind="reminder",
        dedup_key=f"reminder:{t.id}:{d.reminded_at.isoformat()}",
    )
    d.reminder_delivered = None if queued is not None else False
    await s.commit()
    return d.reminder_delivered


async def views(s: AsyncSession, tickets: Sequence[Ticket]) -> dict[int, dict]:
    """Ответственный, срок и первый ответ HR по обращениям — для панели."""
    ids = [t.id for t in tickets]
    deadlines = await s.scalars(select(TicketDeadline).where(TicketDeadline.ticket_id.in_(ids)))
    rows = {d.ticket_id: d for d in deadlines}
    first = select(Message.ticket_id, func.min(Message.created_at)).where(
        Message.ticket_id.in_(ids), Message.author == "hr"
    )
    replies = dict((await s.execute(first.group_by(Message.ticket_id))).all())
    now = datetime.now(UTC)
    out = {}
    for t in tickets:
        d, replied = rows.get(t.id), replies.get(t.id)
        # Обращениям до появления сроков срок считается от времени создания, уведомлений о них не было.
        due = d.due_at if d else deadline(t.created_at, t.severity, _tz(t))
        recruiter = t.employee.recruiter
        out[t.id] = {
            "recruiter": recruiter.name if recruiter else None,
            "due_at": due.isoformat(),
            "first_reply_at": replied.isoformat() if replied else None,
            "overdue": t.status == "open" and replied is None and due <= now,
            "reminded_at": d.reminded_at.isoformat() if d and d.reminded_at else None,
            "reminder_delivered": d.reminder_delivered if d else None,
        }
    return out


async def first_reply(s: AsyncSession, days: int = 30) -> dict:
    """Медиана времени первого ответа HR в рабочих часах; окно — как у остальных показателей «Сегодня»."""
    first = (
        select(Message.ticket_id, func.min(Message.created_at).label("at"))
        .where(Message.author == "hr", Message.ticket_id.is_not(None))
        .group_by(Message.ticket_id)
        .subquery()
    )
    since = datetime.now(UTC) - timedelta(days=days)
    stmt = (
        select(Ticket, first.c.at)
        .join(first, first.c.ticket_id == Ticket.id)
        .where(Ticket.created_at >= since)
    )
    minutes = [work_time(t.created_at, at, _tz(t)) / timedelta(minutes=1) for t, at in await s.execute(stmt)]
    return {"median_min": round(median(minutes)) if minutes else None, "tickets": len(minutes), "days": days}
