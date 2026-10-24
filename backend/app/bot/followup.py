"""Напоминания и сигнал HR относятся к одному сохранённому ожиданию обязательного ответа."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app import channels
from app.channels import ADMIN_FEED, Button, OutMessage
from app.models import Employee, Job

REMINDER_KIND = "required_reminder"
ESCALATION_KIND = "required_escalation"
KINDS = (REMINDER_KIND, ESCALATION_KIND)


async def schedule(s: AsyncSession, emp: Employee, waiting: dict) -> dict:
    from app.bot.scenario import scenario, step

    waiting = {**waiting, "key": uuid4().hex}
    started = datetime.fromisoformat(waiting["started_at"])
    cfg = scenario()["reminder"]
    for days in cfg["after_days"]:
        s.add(
            Job(
                employee_id=emp.id,
                kind=REMINDER_KIND,
                ref=waiting["key"],
                run_at=started + timedelta(days=days),
            )
        )
    s.add(
        Job(
            employee_id=emp.id,
            kind=ESCALATION_KIND,
            ref=waiting["key"],
            run_at=started + timedelta(days=cfg["escalate_after_days"]),
        )
    )
    # При возобновлении опроса старой версии его прежние напоминания заменяются этой очередью.
    current = step(waiting["id"], emp.site_code)
    for message in (current or {}).get("messages", []):
        if message.get("survey"):
            await s.execute(
                update(Job)
                .where(
                    Job.employee_id == emp.id,
                    Job.kind == "reminder",
                    Job.ref == message["survey"],
                    Job.status.in_(["pending", "running"]),
                )
                .values(status="cancelled")
            )
    return waiting


async def remind(s: AsyncSession, emp: Employee, key: str) -> None:
    from app.bot.scenario import render, scenario, step

    waiting = emp.state.get("required_step") or {}
    if waiting.get("key") != key or waiting.get("ticket_id"):
        return
    deadline = datetime.fromisoformat(waiting["started_at"]) + timedelta(
        days=scenario()["reminder"]["escalate_after_days"]
    )
    if datetime.now(UTC) >= deadline:
        return  # после длительного простоя нужен сигнал HR, а не пачка устаревших напоминаний
    current = step(waiting["id"], emp.site_code)
    text = render(scenario()["reminder"]["required_text"], emp)
    await channels.send(
        s,
        emp,
        OutMessage(
            text=f"{text}\n\nШаг: {current['title']}",
            buttons=[[Button("Продолжить ➡️", payload=f"a:required:{key}")]],
        ),
    )


async def resume(s: AsyncSession, emp: Employee, key: str) -> None:
    from app.bot.scenario import run_step

    waiting = emp.state.get("required_step") or {}
    if waiting.get("key") == key:
        await run_step(s, emp, waiting["id"])


async def escalate(s: AsyncSession, emp: Employee, key: str) -> None:
    from app.bot import escalation
    from app.bot.scenario import step

    waiting = emp.state.get("required_step") or {}
    if waiting.get("key") != key or waiting.get("ticket_id"):
        return
    current = step(waiting["id"], emp.site_code)
    ticket = await escalation.queue_ticket(
        s,
        emp,
        "problem",
        f"Не завершён обязательный шаг «{current['title']}». Свяжитесь с сотрудником.",
        "low",
    )
    emp.state = {**emp.state, "required_step": {**waiting, "ticket_id": ticket.id}}
    await s.execute(
        update(Job)
        .where(
            Job.employee_id == emp.id,
            Job.kind == REMINDER_KIND,
            Job.ref == key,
            Job.status.in_(["pending", "running"]),
        )
        .values(status="cancelled")
    )
    # Обращение, задача уведомления и защита от повторной эскалации фиксируются вместе.
    await s.commit()
    channels.hub.publish(ADMIN_FEED, {"type": "ticket", "id": ticket.id, "kind": ticket.kind})
