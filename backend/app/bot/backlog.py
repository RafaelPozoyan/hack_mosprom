"""Просроченная необязательная обратная связь уступает следующему контрольному опросу."""

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Employee, Job


class StepSkipped(Exception):
    """Необязательный запрос больше не актуален; причина сохраняется в очереди."""


def surveys(emp: Employee) -> list[tuple[dict, datetime]]:
    from app.bot.scenario import timeline

    return sorted(
        [(st, at) for st, at in timeline(emp) if at and any(m.get("survey") for m in st.get("messages", []))],
        key=lambda item: item[1],
    )


def skip_reason(emp: Employee, st: dict) -> str | None:
    from app.bot.progress import asks_feedback
    from app.bot.scenario import timeline

    if st.get("required") or not asks_feedback(st):
        return None
    planned = next((at for item, at in timeline(emp) if item["id"] == st["id"]), None)
    if planned is None:
        return None
    checkpoints = surveys(emp)
    cutoff = datetime.now(UTC)
    for checkpoint, at in checkpoints:
        if checkpoint["id"] == emp.state.get("feedback_checkpoint"):
            cutoff = max(cutoff, at)
    for checkpoint, at in checkpoints:
        if planned < at <= cutoff:
            return f"Шаг пропущен: наступил следующий опрос «{checkpoint['title']}»."
    return None


async def check_order(s: AsyncSession, emp: Employee, st: dict) -> None:
    from app.bot.progress import StepBlocked, asks_feedback, required_step
    from app.bot.scenario import step, timeline

    if not asks_feedback(st) or required_step(emp) == st["id"]:
        return
    dates = {item["id"]: at for item, at in timeline(emp)}
    planned = dates.get(st["id"])
    if planned is None:
        return
    jobs = await s.scalars(
        select(Job)
        .where(
            Job.employee_id == emp.id,
            Job.kind == "step",
            Job.ref != st["id"],
        )
        .order_by(Job.id)
    )
    latest = {job.ref: job for job in jobs}
    for job in latest.values():
        if job.status not in ("pending", "running", "blocked", "failed"):
            continue
        previous = step(job.ref, emp.site_code)
        original = dates.get(job.ref)
        if (
            previous
            and previous.get("required")
            and original
            and original < planned
            and original <= datetime.now(UTC)
        ):
            raise StepBlocked(f"Сначала нужно завершить обязательный шаг «{previous['title']}».")


def record_transition(s: AsyncSession, emp: Employee, state: dict, st: dict) -> None:
    from app.bot.scenario import timeline

    checkpoints = surveys(emp)
    new_time = next((at for item, at in checkpoints if item["id"] == st["id"]), None)
    if new_time is None:
        return
    previous_time = next(
        (at for item, at in checkpoints if item["id"] == state.get("feedback_checkpoint")), None
    )
    if previous_time is None or new_time >= previous_time:
        state["feedback_checkpoint"] = st["id"]
    old_survey = (emp.state.get("survey") or {}).get("id")
    self_intro = (emp.state.get("await") or {}).get("kind") == "self_intro"
    for previous, at in timeline(emp):
        if previous.get("required") or previous["id"] == st["id"] or at is None:
            continue
        messages = previous.get("messages", [])
        was_active = (old_survey and any(m.get("survey") == old_survey for m in messages)) or (
            self_intro and any(m.get("await") == "self_intro" for m in messages)
        )
        if was_active:
            s.add(
                Job(
                    employee_id=emp.id,
                    kind="step",
                    ref=previous["id"],
                    run_at=at,
                    status="skipped",
                    done_at=datetime.now(UTC),
                    error=f"Необязательный ответ не завершён: начат опрос «{st['title']}». Полученные ответы сохранены.",
                )
            )
