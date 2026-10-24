"""Ожидание обязательной обратной связи хранится в состоянии сотрудника, отдельно от отправки."""

from datetime import UTC, datetime

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import backlog
from app.models import Employee, Job


class StepBlocked(Exception):
    """Новый запрос обратной связи должен дождаться текущего обязательного ответа."""


def required_step(emp: Employee) -> str | None:
    from app.bot.scenario import scenario, step

    if emp.state.get("required_step"):
        return emp.state["required_step"]["id"]
    # При обновлении уже начатый обязательный опрос также должен продолжить ожидание.
    survey_id = (emp.state.get("survey") or {}).get("id")
    if survey_id:
        for candidate in scenario()["steps"]:
            current = step(candidate["id"], emp.site_code)
            if not current or not current.get("required"):
                continue
            if any(message.get("survey") == survey_id for message in current.get("messages", [])):
                return current["id"]
    return None


def asks_feedback(st: dict) -> bool:
    return bool(
        st.get("required")
        or any(message.get("survey") or message.get("await") for message in st.get("messages", []))
    )


def check_step(emp: Employee, st: dict) -> None:
    from app.bot.scenario import step

    reason = backlog.skip_reason(emp, st)
    if reason:
        raise backlog.StepSkipped(reason)
    waiting = required_step(emp)
    if waiting and waiting != st["id"] and asks_feedback(st):
        previous = step(waiting, emp.site_code)
        title = previous.get("title", waiting) if previous else waiting
        raise StepBlocked(f"Сначала нужен ответ на шаг «{title}». Информация и помощь HR остаются доступны.")


async def begin(s: AsyncSession, emp: Employee, st: dict) -> None:
    state = dict(emp.state)
    backlog.record_transition(s, emp, state, st)
    if asks_feedback(st) and (state.get("await") or {}).get("kind") == "self_intro":
        state.pop("await")
    current_survey = (state.get("survey") or {}).get("id")
    same_survey = any(message.get("survey") == current_survey for message in st.get("messages", []))
    if asks_feedback(st) and current_survey and not same_survey:
        state.pop("survey")
        if (state.get("await") or {}).get("kind") in ("survey_comment", "survey_text"):
            state.pop("await")
        await s.execute(
            update(Job)
            .where(
                Job.employee_id == emp.id,
                Job.kind == "reminder",
                Job.ref == current_survey,
                Job.status == "pending",
            )
            .values(status="cancelled")
        )
    if st.get("required") and not state.get("required_step"):
        state["required_step"] = {"id": st["id"], "started_at": datetime.now(UTC).isoformat()}
    if state != emp.state:
        emp.state = state
        await s.commit()


async def complete(s: AsyncSession, emp: Employee, step_id: str) -> None:
    """Снять ожидание и разбудить отложенные шаги в одной транзакции с сохранением ответа."""
    if required_step(emp) != step_id:
        return
    emp.state = {key: value for key, value in emp.state.items() if key != "required_step"}
    await s.execute(
        update(Job)
        .where(Job.employee_id == emp.id, Job.kind == "step", Job.status == "blocked")
        .values(status="pending", error=None)
    )
