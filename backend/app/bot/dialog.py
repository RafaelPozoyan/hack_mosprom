"""Маршрутизация входящих событий: гибрид кнопочного сценария и свободного диалога с ИИ."""

import logging
import re
from datetime import date, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import channels
from app.analytics.sentiment import analyze_later
from app.bot import escalation, surveys
from app.bot.scenario import button_rows, find_button, render, run_step, scenario, schedule_employee, site_tz
from app.channels import Button, InEvent, OutMessage
from app.models import Employee, Job, Message, Question, SurveyAnswer, Ticket
from app.rag.answer import answer as rag_answer

log = logging.getLogger(__name__)

SMALLTALK = {
    "привет", "приветик", "здравствуй", "здравствуйте", "добрый", "доброе", "день", "утро", "вечер", "хай", "hello",
    "hi", "спасибо", "спс", "благодарю", "пока", "ок", "окей", "ok", "хорошо", "понятно", "ясно", "отлично", "супер",
    "корсик", "да", "нет", "ага", "угу", "класс", "круто",
}


def is_smalltalk(text: str) -> bool:
    words = re.findall(r"[a-zа-яё]+", text.lower())
    return all(w in SMALLTALK for w in words)


async def handle(s: AsyncSession, emp: Employee, ev: InEvent) -> None:
    if ev.kind == "start":
        await on_start(s, emp)
    elif ev.kind == "callback":
        await on_callback(s, emp, ev)
    else:
        await on_text(s, emp, ev.text.strip())


async def on_start(s: AsyncSession, emp: Employee) -> None:
    if emp.status == "invited":
        emp.status = "active"
        emp.started_at = datetime.now(ZoneInfo("UTC"))
        await s.commit()
        await schedule_employee(s, emp)
        await run_step(s, emp, "welcome")
    elif not await s.scalar(select(Message.id).where(Message.employee_id == emp.id, Message.step_id == "welcome").limit(1)):
        # приветствие не ушло (сбой отправки в MAX): повторный старт начинает путь, а не показывает меню
        await run_step(s, emp, "welcome")
    else:
        await send_menu(s, emp)


async def on_text(s: AsyncSession, emp: Employee, text: str) -> None:
    if not text:
        return
    await channels.log_incoming(s, emp, text)
    if text.startswith("/"):
        await command(s, emp, text[1:].split()[0].lower())
        return
    aw = (emp.state.get("await") or {}).get("kind")
    if aw in ("survey_comment", "survey_text"):
        await surveys.comment(s, emp, text)
    elif aw == "relay":
        await escalation.open_dialog(s, emp, text)
    elif aw == "self_intro":
        emp.state = {k: v for k, v in emp.state.items() if k != "await"}
        await s.commit()
        from app.bot.scenario import notify_audience

        await notify_audience(s, emp, "recruiter", f"Самопрезентация сотрудника для знакомства с коллегами:\n\n{text}", "d2_self_intro")
        await channels.send(s, emp, OutMessage(text=render("Спасибо! Передал рекрутеру — скоро {тебя|вас} представят коллегам 🙌", emp)))
    elif emp.state.get("ticket_id"):
        await escalation.forward_from_employee(s, emp, text)
    elif is_smalltalk(text):
        await send_menu(s, emp, intro=render("Я здесь 🐾 ", emp))
    else:
        await answer_question(s, emp, text)


async def answer_question(s: AsyncSession, emp: Employee, text: str, log_question: bool = True) -> None:
    form = "вы" if emp.address_form == "vy" else "ты"
    res = await rag_answer(s, text, emp.site_code, form)
    q = Question(
        employee_id=emp.id, text=text, answered=res.answered, reason=res.reason, best_score=res.best_score,
        answer=res.text, sources=res.sources,
    )
    s.add(q)
    await s.commit()
    if res.answered:
        refs = list(dict.fromkeys(src["heading"] for src in res.sources))
        body = res.text + "\n\n📄 " + "\n📄 ".join(refs)
        await channels.send(
            s,
            emp,
            OutMessage(
                text=body,
                sources=res.sources,
                buttons=[
                    [Button("👍", payload=f"f:{q.id}:1"), Button("👎", payload=f"f:{q.id}:-1")],
                    [Button("🧑‍💼 Уточнить у HR", payload=f"a:relay_q:{q.id}")],
                ],
            ),
        )
    else:
        fb = scenario()["fallback"]
        emp.state = {**emp.state, "last_problem": text}
        await s.commit()
        await channels.send(
            s,
            emp,
            OutMessage(text=render(fb["text"], emp), image=fb.get("image"), buttons=surveys.offer_buttons(emp, ["relay", "recruiter"])),
        )
    if log_question:
        analyze_later(emp.id, text, "chat", question_id=q.id)


async def command(s: AsyncSession, emp: Employee, cmd: str) -> None:
    target = scenario()["commands"].get(cmd)
    if target in (None, "menu"):
        await send_menu(s, emp)
    elif target == "relay_hr":
        await escalation.ask_relay(s, emp)
    else:
        await run_step(s, emp, target)


async def send_menu(s: AsyncSession, emp: Employee, intro: str = "") -> None:
    m = scenario()["menu"]
    rows = [
        [Button(render(b["text"], emp), payload=f"b:menu:0:{r}:{c}") for c, b in enumerate(row)]
        for r, row in enumerate(button_rows(m["buttons"]))
    ]
    await channels.send(s, emp, OutMessage(text=intro + render(m["text"], emp), buttons=rows))


async def on_callback(s: AsyncSession, emp: Employee, ev: InEvent) -> None:
    p = ev.payload
    if ev.text:
        await channels.log_incoming(s, emp, ev.text, {"button": True})
    kind, _, rest = p.partition(":")
    if kind == "b":
        await scenario_button(s, emp, p)
    elif kind == "s":
        sid, qid, val = rest.split(":")
        await surveys.answer(s, emp, sid, qid, val)
    elif kind == "m":
        sid, qid, opt = rest.split(":")
        msg = await find_message(s, emp, ev.message_mid)
        await surveys.toggle(s, emp, sid, qid, opt, msg)
    elif kind == "f":
        qid, val = rest.split(":")
        q = await s.get(Question, int(qid))
        if q and q.employee_id == emp.id:
            q.feedback = int(val)
            await s.commit()
        if int(val) < 0:
            await channels.send(s, emp, OutMessage(
                text=render("Спасибо, что {отметил|отметили}! {Хочешь|Хотите}, передам вопрос HR — они ответят точнее?", emp),
                buttons=[[Button("📨 Да, передай", payload=f"a:relay_q:{qid}")]],
            ))
    elif kind == "a":
        await action(s, emp, rest)


async def scenario_button(s: AsyncSession, emp: Employee, payload: str) -> None:
    btn = find_button(payload, emp)
    if btn is None:
        return
    _, step_id, idx, *_ = payload.split(":")
    if step_id != "menu" and any(btn.get(k) for k in ("set_form", "flag", "goto", "action")):
        # такие кнопки меняют путь новичка: двойной тап или нажатие в старом сообщении не должны срабатывать повторно
        pressed = emp.state.get("pressed", [])
        if f"{step_id}:{idx}" in pressed:
            return
        emp.state = {**emp.state, "pressed": [*pressed, f"{step_id}:{idx}"]}
        await s.commit()
    if btn.get("set_form"):
        emp.address_form = btn["set_form"]
        await s.commit()
    if btn.get("flag"):
        s.add(SurveyAnswer(employee_id=emp.id, survey_id=f"step:{step_id}", question_id=step_id,
                           value={"label": btn["text"], "flag": btn["flag"]}, flagged=True))
        await s.commit()
        from app.analytics.sentiment import react

        await react(s, emp, [btn["flag"]], "medium", f"Ответ на шаг «{step_id}»: {btn['text']}")
    if btn.get("reply"):
        await channels.send(s, emp, OutMessage(text=render(btn["reply"], emp)))
    if btn.get("goto"):
        await run_step(s, emp, btn["goto"])
    if btn.get("action"):
        await action(s, emp, btn["action"])


async def action(s: AsyncSession, emp: Employee, act: str) -> None:
    name, _, arg = act.partition(":")
    if name in ("relay", "relay_hr"):
        await escalation.ask_relay(s, emp)
    elif name == "relay_q":
        q = await s.get(Question, int(arg))
        await escalation.open_dialog(s, emp, f"Вопрос сотрудника: {q.text}" if q else "Вопрос сотрудника", kind="question")
    elif name == "recruiter":
        await escalation.open_dialog(s, emp, "Сотрудник просит связаться с рекрутером", kind="problem")
    elif name == "recruiter_voice":
        problem = emp.state.get("last_problem") or ""
        await escalation.create_ticket(s, emp, "problem", f"Готов(а) озвучить причину сомнений лично рекрутеру. {problem}".strip())
        await channels.send(s, emp, OutMessage(text=render("Передал рекрутеру — он свяжется с {тобой|вами} лично 🙌", emp)))
    elif name == "close_dialog":
        tid = emp.state.get("ticket_id")
        if tid:
            t = await s.get(Ticket, tid)
            await escalation.close(s, t)
    elif name == "cancel":
        emp.state = {k: v for k, v in emp.state.items() if k != "await"}
        await s.commit()
        await send_menu(s, emp, intro="Хорошо, отменил. ")
    elif name == "skip_comment":
        await surveys.comment(s, emp, None)
    elif name == "resume":
        await surveys.resume(s, emp)
    elif name == "progress":
        await send_progress(s, emp)
    elif name == "menu":
        await send_menu(s, emp)


async def send_progress(s: AsyncSession, emp: Employee) -> None:
    total = (emp.probation_end - emp.start_date).days + 1
    day = (date.today() - emp.start_date).days + 1
    jobs = (
        await s.scalars(
            select(Job).where(Job.employee_id == emp.id, Job.kind == "step", Job.status == "pending").order_by(Job.run_at).limit(3)
        )
    ).all()
    titles = {st["id"]: st for st in scenario()["steps"]}
    lines = []
    if day < 1:
        lines.append(f"До выхода на работу: {1 - day} дн. 🗓")
    else:
        filled = min(10, round(10 * day / total))
        bar = "🟩" * filled + "⬜" * (10 - filled)
        lines.append(f"День {min(day, total)} из {total} испытательного срока\n{bar}")
    upcoming = [j for j in jobs if titles.get(j.ref, {}).get("audience", "employee") == "employee"]
    if upcoming:
        lines.append("\nВпереди:")
        for j in upcoming:
            lines.append(f"• {j.run_at.astimezone(site_tz(emp.site_code)).strftime('%d.%m')} — {titles.get(j.ref, {}).get('title', 'сообщение от Корсика')}")
    await channels.send(s, emp, OutMessage(text="\n".join(lines)))




async def find_message(s: AsyncSession, emp: Employee, mid: str | None) -> Message | None:
    if not mid:
        return None
    if mid.startswith("web:"):
        return await s.get(Message, int(mid[4:]))
    return await s.scalar(select(Message).where(Message.employee_id == emp.id, Message.max_mid == mid))
