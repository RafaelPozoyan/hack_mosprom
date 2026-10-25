"""Демо-доступ «попробуйте сами»: QR на слайде защиты заводит новичка на каждого пользователя MAX.

Время ускорено: шаг таймлайна приходит по кнопке «⏩ Дальше», а подсказка говорит, когда он пришёл бы по плану, —
так предложил показывать сценарий заказчик на чек-поинте 24.10. Всё остальное — тот же движок: сценарий, ответы ИИ,
опросы, эскалация на HR. В проде выключено (DEMO_TRY_PAYLOAD пустой): новички приходят из панели и кадровой системы.
"""

import asyncio
import logging
import secrets
from datetime import date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app import channels
from app.bot.scenario import add_months, next_workday, site_tz, step
from app.channels import Button, OutMessage
from app.config import settings
from app.db import session
from app.models import Employee, Job, Message, Staff

log = logging.getLogger(__name__)

NEXT = "demo:next"
NEXT_TEXTS = {"/next", "дальше"}
WEEKDAYS = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")
INTRO = (
    "Это демо-доступ, время здесь ускорено. Нажимайте «⏩ Дальше» — и Корсик пройдёт с вами путь новичка "
    "от оффера до конца испытательного срока за пару минут. В любой момент можно спросить что-нибудь своими словами."
)
FULL = "Демо-места закончились — спасибо за интерес к Корсику! 🐾"
DONE = "Это был последний шаг пути новичка. Спасибо, что попробовали Корсика! 🐾"
SURVEY_FIRST = "Сначала ответьте на вопрос опроса выше — Корсик ждёт ответа."
REQUIRED_FIRST = "Сначала ответьте на шаг «{title}» — он обязательный, без ответа Корсик дальше не пойдёт."
HINT_MARK = "Дальше по плану"
STAFF_LABEL = {"to_manager": "руководителю", "to_recruiter": "рекрутеру"}

# Двойное нажатие «Дальше» не должно запускать два шага сразу
_advancing: set[int] = set()


def is_try(payload: str) -> bool:
    return bool(settings().demo_try_payload) and payload == settings().demo_try_payload


def _plural(n: int, one: str, few: str, many: str) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return one
    return few if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14) else many


def plan_label(run_at: datetime, emp: Employee) -> str:
    """Когда шаг пришёл бы по плану, в часовом поясе площадки: «за день до выхода, пн 26.10 10:00»."""
    local = run_at.astimezone(site_tz(emp.site_code))
    days = (local.date() - emp.start_date).days
    if days == -1:
        rel = "за день до выхода"
    elif days < 0:
        rel = f"за {-days} {_plural(-days, 'день', 'дня', 'дней')} до выхода"
    else:
        rel = f"{days + 1}-й день работы"
    return f"{rel}, {WEEKDAYS[local.weekday()]} {local:%d.%m %H:%M}"


async def enroll(user: dict) -> int | None:
    """Новичок для пользователя MAX; повторный скан возвращает того же. None — демо-места закончились."""
    cfg = settings()
    async with session() as s:
        if emp_id := await s.scalar(select(Employee.id).where(Employee.max_user_id == user["user_id"])):
            return emp_id
        taken = await s.scalar(
            select(func.count()).select_from(Employee).where(Employee.state["demo"].as_boolean())
        )
        if taken >= cfg.demo_try_limit:
            return None
        first = (user.get("first_name") or user.get("name") or "Гость").strip()
        start = next_workday(
            date.today() + timedelta(days=3)
        )  # скан QR с оффера — «за 2–3 дня до выхода» (ТЗ)
        recruiter = await s.scalar(
            select(Staff.id).where(Staff.role == "recruiter").order_by(Staff.id).limit(1)
        )
        manager = await s.scalar(select(Staff.id).where(Staff.role == "manager").order_by(Staff.id).limit(1))
        emp = Employee(
            full_name=" ".join(x for x in (user.get("last_name"), first) if x),
            first_name=first,
            position="Оператор экструзионной линии",  # для письма руководителю; демо-признак — state["demo"]
            site_code=cfg.demo_try_site,
            start_date=start,
            probation_end=add_months(start, cfg.probation_months),
            recruiter_id=recruiter,
            manager_id=manager,
            channel="max",
            max_user_id=user["user_id"],
            invite_code=secrets.token_urlsafe(9),
            state={"demo": True},
        )
        s.add(emp)
        try:
            await s.commit()
        except IntegrityError:  # тот же пользователь отсканировал QR дважды одновременно
            await s.rollback()
            return await s.scalar(select(Employee.id).where(Employee.max_user_id == user["user_id"]))
        return emp.id


async def is_demo(emp_id: int) -> bool:
    async with session() as s:
        emp = await s.get(Employee, emp_id)
        return bool(emp and emp.state.get("demo"))


def schedule_hint(emp_id: int, intro: bool = False) -> None:
    asyncio.create_task(hint(emp_id, intro))


def schedule_after_event(emp_id: int) -> None:
    asyncio.create_task(after_event(emp_id))


def start_advance(emp_id: int) -> None:
    if emp_id not in _advancing:
        _advancing.add(emp_id)
        asyncio.create_task(_advance(emp_id))


async def _advance(emp_id: int) -> None:
    """Следующий шаг сразу — та же перемотка, что в демо-режиме панели, — и подсказка о следующем."""
    from app.scheduler import run_job  # планировщик импортирует бота: локальный импорт без цикла

    try:
        if await _required_first(emp_id):
            return
        async with session() as s:
            job = await s.scalar(
                select(Job)
                .where(
                    Job.employee_id == emp_id,
                    Job.status.in_(["pending", "running"]),
                    Job.kind.in_(["step", "reminder"]),
                )
                .order_by(Job.run_at, Job.id)
                .limit(1)
                .with_for_update()
            )
            if job is not None:
                # Перемотка сохраняется до исполнения; после сбоя шаг подберёт планировщик
                job.status = "running"
                await s.commit()
        if job is not None:
            async with session() as s:
                seen = await s.scalar(select(func.max(Message.id)).where(Message.employee_id == emp_id)) or 0
            await run_job(job.id)
            await _show_staff_notes(emp_id, seen)
        await hint(emp_id)
    except Exception:
        log.exception("demo advance failed: employee=%s", emp_id)
    finally:
        _advancing.discard(emp_id)


async def hint(emp_id: int, intro: bool = False) -> None:
    """Что и когда придёт дальше по плану — с кнопкой «⏩ Дальше». Только для демо-новичков."""
    async with session() as s:
        emp = await s.get(Employee, emp_id)
        if emp is None or not emp.state.get("demo"):
            return
        job = await s.scalar(
            select(Job)
            .where(Job.employee_id == emp_id, Job.status == "pending", Job.kind.in_(["step", "reminder"]))
            .order_by(Job.run_at, Job.id)
            .limit(1)
        )
        if job is None:
            await channels.send(s, emp, OutMessage(text=DONE))
            return
        title = (
            (step(job.ref, emp.site_code) or {}).get("title", job.ref)
            if job.kind == "step"
            else "Напоминание об опросе"
        )
        note = settings().demo_try_note
        lines = [f"{INTRO} {note}" if note else INTRO] if intro else []
        if emp.state.get("survey"):
            lines.append(SURVEY_FIRST)
        elif waiting := _waiting_required(emp):
            lines.append(REQUIRED_FIRST.format(title=_step_title(waiting["id"], emp)))
        lines.append(f"⏩ Дальше по плану: «{title}» — {plan_label(job.run_at, emp)}")
        await channels.send(
            s, emp, OutMessage(text="\n\n".join(lines), buttons=[[Button("⏩ Дальше", payload=NEXT)]])
        )


def _step_title(step_id: str, emp: Employee) -> str:
    return (step(step_id, emp.site_code) or {}).get("title", step_id)


def _waiting_required(emp: Employee) -> dict | None:
    """Обязательный шаг ждёт ответа; опрос в процессе ведёт свой счёт вопросов и сюда не относится."""
    waiting = emp.state.get("required_step") or {}
    return waiting if waiting.get("key") and not emp.state.get("survey") else None


async def _required_first(emp_id: int) -> bool:
    """«Дальше» не перепрыгивает обязательный шаг, а возвращает к нему: иначе следующий опрос молча заблокируется."""
    async with session() as s:
        emp = await s.get(Employee, emp_id)
        waiting = _waiting_required(emp) if emp else None
        if waiting is None:
            return False
        await channels.send(
            s,
            emp,
            OutMessage(
                text=REQUIRED_FIRST.format(title=_step_title(waiting["id"], emp)),
                buttons=[[Button("Продолжить ➡️", payload=f"a:required:{waiting['key']}")]],
            ),
        )
        return True


async def _show_staff_notes(emp_id: int, after_id: int) -> None:
    """Шаг ушёл руководителю или рекрутеру — демо-новичок видит, что именно они получили: так видна вся цепочка."""
    async with session() as s:
        emp = await s.get(Employee, emp_id)
        notes = (
            await s.scalars(
                select(Message)
                .where(Message.employee_id == emp_id, Message.id > after_id, Message.author.in_(STAFF_LABEL))
                .order_by(Message.id)
            )
        ).all()
        for m in notes:
            text = f"📨 Это сообщение Корсик отправил {STAFF_LABEL[m.author]}, а не вам:\n\n{m.text}"
            await channels.send(s, emp, OutMessage(text=text))


async def after_event(emp_id: int) -> None:
    """После ответа демо-новичка: цепочка шагов закончилась и Корсик ничего не ждёт — снова «Дальше» внизу чата."""
    async with session() as s:
        emp = await s.get(Employee, emp_id)
        if emp is None or not emp.state.get("demo") or emp.state.get("survey") or emp.state.get("await"):
            return
        last = await s.scalar(
            select(Message)
            .where(Message.employee_id == emp_id, Message.direction == "out")
            .order_by(Message.id.desc())
            .limit(1)
        )
    # Ответ ИИ (без шага) не перебиваем подсказкой; подсказку подряд не повторяем
    if last is not None and last.step_id and HINT_MARK not in last.text:
        await hint(emp_id)
