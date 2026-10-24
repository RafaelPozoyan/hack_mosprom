"""Уточнение контекста свободного ответа: текст не относится к опросу без подтверждения сотрудника."""

from sqlalchemy.ext.asyncio import AsyncSession

from app import channels
from app.bot import surveys
from app.bot.scenario import render
from app.channels import Button, OutMessage
from app.models import Employee, Message


async def propose(s: AsyncSession, emp: Employee, message: Message) -> None:
    awaiting = emp.state.get("await") or {}
    current = emp.state.get("survey")
    if awaiting.get("kind") not in ("survey_comment", "survey_text") or not current:
        return
    question = surveys.questions(emp, current["id"])[current["q"]]
    emp.state = {**emp.state, "await": {**awaiting, "candidate_id": message.id}}
    await s.commit()
    await channels.send(
        s,
        emp,
        OutMessage(
            text=render("Сохранить последнее сообщение как ответ на этот вопрос?", emp)
            + "\n\n"
            + render(question["text"], emp),
            buttons=[
                [Button("Да, это ответ", payload=f"a:confirm_comment:{message.id}")],
                [Button("Это отдельный вопрос", payload=f"a:question_comment:{message.id}")],
            ],
        ),
    )


async def confirm(s: AsyncSession, emp: Employee, candidate: str, *, as_question: bool = False) -> None:
    from app.bot.dialog import answer_question

    awaiting = emp.state.get("await") or {}
    if (
        awaiting.get("kind") not in ("survey_comment", "survey_text")
        or not awaiting.get("candidate_id")
        or str(awaiting["candidate_id"]) != candidate
    ):
        await surveys.stale_button(s, emp)
        return
    message = await s.get(Message, awaiting["candidate_id"])
    if not message or message.employee_id != emp.id or message.author != "employee":
        return
    if as_question:
        emp.state = {**emp.state, "await": {k: v for k, v in awaiting.items() if k != "candidate_id"}}
        await s.commit()
        await answer_question(s, emp, message.text)
    else:
        await surveys.comment(s, emp, message.text)


async def skip(s: AsyncSession, emp: Employee, answer_id: str) -> None:
    awaiting = emp.state.get("await") or {}
    if (
        awaiting.get("kind") != "survey_comment"
        or not awaiting.get("answer_id")
        or str(awaiting["answer_id"]) != answer_id
    ):
        await surveys.stale_button(s, emp)
        return
    await surveys.comment(s, emp, None)
