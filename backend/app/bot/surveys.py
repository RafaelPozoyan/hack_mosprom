"""Опросы из ТЗ: выбор, да/нет, шкала 1–10 с комментарием ниже порога, множественный выбор, открытый ответ."""

from datetime import datetime, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app import channels
from app.analytics.sentiment import analyze_later, react
from app.bot import progress
from app.bot.scenario import render, run_step, scenario, step
from app.channels import Button, OutMessage
from app.db import after_commit
from app.models import Employee, Job, Message, SurveyAnswer

COMMENT_PROMPT = "Пожалуйста, {укажи|укажите} причину — это поможет нам сделать адаптацию лучше 🙏"
SKIP = "Пропустить ➡️"


def survey_def(survey_id: str) -> dict:
    return scenario()["surveys"][survey_id]


def questions(emp: Employee, survey_id: str) -> list[dict]:
    return survey_def(survey_id)["questions"]


def options(emp: Employee, q: dict) -> list[dict]:
    return [o for o in q.get("options", []) if not o.get("sites") or emp.site_code in o["sites"]]


def offer_buttons(emp: Employee, offer: list[str]) -> list[list[Button]]:
    rows = []
    for o in offer or []:
        if o == "relay":
            rows.append([Button("📨 Передать проблему через Корсика", payload="a:relay")])
        elif o == "recruiter":
            link = emp.recruiter.max_link if emp.recruiter else None
            rows.append(
                [
                    Button("🧑‍💼 Связаться с рекрутером", url=link)
                    if link
                    else Button("🧑‍💼 Связаться с рекрутером", payload="a:recruiter")
                ]
            )
        elif o == "recruiter_voice":
            rows.append([Button("Готов(а) озвучить причину лично рекрутеру", payload="a:recruiter_voice")])
    return rows


def question_message(emp: Employee, survey_id: str, q: dict, selected: list[str] | None = None) -> OutMessage:
    key = emp.state["survey"]["key"]
    prefix = f"s:{survey_id}:{q['id']}:{key}"
    text = render(q["text"], emp)
    kind = q["type"]
    if kind == "choice":
        rows = [[Button(render(o["text"], emp), payload=f"{prefix}:{o['id']}")] for o in options(emp, q)]
    elif kind == "yesno":
        rows = [[Button("Да", payload=f"{prefix}:yes"), Button("Нет", payload=f"{prefix}:no")]]
    elif kind == "scale":
        text += f"\n\n1 — {q['min_label']}, 10 — {q['max_label']}"
        rows = [[Button(str(n), payload=f"{prefix}:{n}") for n in range(r, r + 5)] for r in (1, 6)]
    elif kind == "multi":
        selected = selected or []
        rows = [
            [
                Button(
                    ("✅ " if o["id"] in selected else "") + render(o["text"], emp),
                    payload=f"m:{survey_id}:{q['id']}:{key}:{o['id']}",
                )
            ]
            for o in options(emp, q)
        ]
        rows.append([Button("Готово ➡️", payload=f"{prefix}:done")])
    else:  # text
        rows = [[Button(SKIP, payload=f"{prefix}:skip")]]
    return OutMessage(text=text, buttons=rows)


async def start(s: AsyncSession, emp: Employee, survey_id: str) -> None:
    # недописанный комментарий к прошлому опросу не должен перехватывать ответы нового
    state = {
        k: v
        for k, v in emp.state.items()
        if not (k == "await" and v.get("kind") in ("survey_comment", "survey_text"))
    }
    emp.state = {**state, "survey": {"id": survey_id, "q": 0, "selected": [], "key": uuid4().hex}}
    await s.commit()
    await _ask(s, emp)
    now = datetime.now(ZoneInfo("UTC"))
    scheduled = (emp.state.get("required_step") or {}).get("key")
    days = [] if scheduled else scenario()["reminder"]["after_days"]
    for d in days:
        s.add(Job(employee_id=emp.id, kind="reminder", ref=survey_id, run_at=now + timedelta(days=d)))
    await s.commit()


async def _ask(s: AsyncSession, emp: Employee) -> None:
    st = emp.state["survey"]
    if not st.get("key"):
        st = {**st, "key": uuid4().hex}
        emp.state = {**emp.state, "survey": st}
        await s.commit()
    q = questions(emp, st["id"])[st["q"]]
    if q["type"] == "text":
        emp.state = {**emp.state, "await": {"kind": "survey_text"}}
        await s.commit()
    await channels.send(s, emp, question_message(emp, st["id"], q), step_id=f"survey:{st['id']}")


def current(emp: Employee, survey_id: str, qid: str) -> dict | None:
    st = emp.state.get("survey")
    if not st or st["id"] != survey_id:
        return None
    q = questions(emp, survey_id)[st["q"]]
    return q if q["id"] == qid else None


async def toggle(
    s: AsyncSession, emp: Employee, survey_id: str, qid: str, opt: str, msg: Message | None
) -> None:
    q = current(emp, survey_id, qid)
    if not q:
        return
    st = dict(emp.state["survey"])
    exclusive = {o["id"] for o in options(emp, q) if o.get("exclusive")}
    sel = set(st.get("selected", []))
    if opt in sel:
        sel.discard(opt)
    elif opt in exclusive:
        sel = {opt}
    else:
        sel = (sel - exclusive) | {opt}
    st["selected"] = sorted(sel)
    emp.state = {**emp.state, "survey": st}
    await s.commit()
    if msg:
        await channels.edit(s, emp, msg, question_message(emp, survey_id, q, st["selected"]))


async def answer(s: AsyncSession, emp: Employee, survey_id: str, qid: str, raw: str) -> None:
    q = current(emp, survey_id, qid)
    if not q:
        await channels.send(s, emp, OutMessage(text="Этот вопрос уже позади — спасибо! 🙂"))
        return
    aw = emp.state.get("await") or {}
    if aw.get("kind") == "survey_comment" and aw.get("qid") == qid:
        return  # ответ на этот вопрос уже записан, ждём комментарий — повторный тап не создаёт второй ответ
    kind = q["type"]
    reply, ask, offer, flag = None, None, [], None
    if kind == "choice":
        opt = next(o for o in options(emp, q) if o["id"] == raw)
        value = {"option": raw, "label": opt["text"]}
        reply, ask, offer, flag = opt.get("reply"), opt.get("ask"), opt.get("offer", []), opt.get("flag")
    elif kind == "yesno":
        value = {"answer": raw}
        if raw == q.get("comment_on"):
            ask, flag = COMMENT_PROMPT, q.get("flag")
    elif kind == "scale":
        value = {"score": int(raw)}
        if int(raw) < q.get("comment_below", 0):
            ask = COMMENT_PROMPT
    elif kind == "multi":
        sel = emp.state["survey"].get("selected", [])
        chosen = [o for o in options(emp, q) if o["id"] in sel]
        value = {"options": sel, "labels": [o["text"] for o in chosen]}
        for o in chosen:
            reply, offer, flag = o.get("reply", reply), o.get("offer", offer), o.get("flag", flag)
    else:  # text: пропуск
        value = {"text": None}
        emp.state = {k: v for k, v in emp.state.items() if k != "await"}
    if flag:
        value["flag"] = flag
    if q.get("topic"):
        value["topic"] = q["topic"]
    ans = SurveyAnswer(
        employee_id=emp.id, survey_id=survey_id, question_id=qid, value=value, flagged=bool(flag)
    )
    s.add(ans)
    await s.commit()
    if flag and not ask:
        await react(s, emp, [flag], "medium", f"{render(q['text'], emp)} → {value.get('label') or value}")

    if ask:
        emp.state = {
            **emp.state,
            "await": {"kind": "survey_comment", "answer_id": ans.id, "qid": qid, "offer": offer},
        }
        await s.commit()
        await channels.send(
            s,
            emp,
            OutMessage(text=render(ask, emp), buttons=[[Button(SKIP, payload=f"a:skip_comment:{ans.id}")]]),
        )
        return
    if reply or offer:
        await channels.send(
            s,
            emp,
            OutMessage(text=render(reply or "Понял {тебя|вас}!", emp), buttons=offer_buttons(emp, offer)),
        )
    await advance(s, emp)


async def comment(s: AsyncSession, emp: Employee, text: str | None) -> None:
    """Свободный ответ: комментарий к оценке, «свой вариант» или открытый вопрос опроса."""
    aw = emp.state.get("await") or {}
    if aw.get("kind") not in ("survey_comment", "survey_text"):
        return  # «Пропустить» из старого сообщения: комментарий уже не ждём, иначе опрос перескочил бы вопрос
    emp.state = {k: v for k, v in emp.state.items() if k != "await"}
    await s.commit()
    if aw.get("kind") == "survey_text":
        st = emp.state["survey"]
        q = questions(emp, st["id"])[st["q"]]
        s.add(
            SurveyAnswer(
                employee_id=emp.id,
                survey_id=st["id"],
                question_id=q["id"],
                value={"text": text},
                comment=text,
            )
        )
        await s.commit()
    elif aw.get("answer_id") and text:
        ans = await s.get(SurveyAnswer, aw["answer_id"])
        if ans:
            ans.comment = text
            await s.commit()
    flag = None
    if aw.get("answer_id"):
        ans = await s.get(SurveyAnswer, aw["answer_id"])
        flag = ans.value.get("flag") if ans else None
    if text:
        after_commit(s, analyze_later, emp.id, text, "survey", [flag] if flag else None)
    elif flag:
        await react(s, emp, [flag], "medium", f"Ответ в опросе: {flag}")
    if aw.get("offer") and text:
        await channels.send(
            s,
            emp,
            OutMessage(
                text=render("Спасибо, что {поделился|поделились}! Если нужно — передам это коллегам:", emp),
                buttons=offer_buttons(emp, aw["offer"]),
            ),
        )
    if aw.get("offer"):
        emp.state = {**emp.state, "last_problem": text}
        await s.commit()
    await advance(s, emp)


async def advance(s: AsyncSession, emp: Employee) -> None:
    st = dict(emp.state.get("survey") or {})
    if not st:
        return
    st["q"] += 1
    st["selected"] = []
    qs = questions(emp, st["id"])
    if st["q"] < len(qs):
        emp.state = {**emp.state, "survey": st}
        await s.commit()
        await _ask(s, emp)
        return
    sdef = survey_def(st["id"])
    waiting = progress.required_step(emp)
    waiting_step = step(waiting, emp.site_code) if waiting else None
    waiting_for_this_survey = waiting_step and any(
        message.get("survey") == st["id"] for message in waiting_step.get("messages", [])
    )
    if waiting_for_this_survey:
        await progress.complete(s, emp, waiting)
    emp.state = {k: v for k, v in emp.state.items() if k not in ("survey", "await")}
    await s.execute(
        update(Job)
        .where(
            Job.employee_id == emp.id, Job.kind == "reminder", Job.ref == st["id"], Job.status == "pending"
        )
        .values(status="cancelled")
    )
    await s.commit()
    if sdef.get("outro"):
        await channels.send(s, emp, OutMessage(text=render(sdef["outro"], emp)))
    if sdef.get("then"):
        await run_step(s, emp, sdef["then"])


async def remind(s: AsyncSession, emp: Employee, survey_id: str) -> None:
    st = emp.state.get("survey")
    if not st or st["id"] != survey_id:
        return
    r = scenario()["reminder"]
    await channels.send(
        s,
        emp,
        OutMessage(
            text=render(r["text"], emp),
            image=r.get("image"),
            buttons=[[Button("Продолжить опрос ➡️", payload="a:resume")]],
        ),
    )


async def resume(s: AsyncSession, emp: Employee) -> None:
    if not emp.state.get("survey"):
        return
    if (emp.state.get("await") or {}).get("kind") == "survey_comment":
        await channels.send(
            s,
            emp,
            OutMessage(
                text=render(COMMENT_PROMPT, emp),
                buttons=[[Button(SKIP, payload=f"a:skip_comment:{emp.state['await']['answer_id']}")]],
            ),
        )
        return
    await _ask(s, emp)


async def stale_button(s: AsyncSession, emp: Employee) -> None:
    await channels.send(
        s,
        emp,
        OutMessage(
            text="Эта кнопка относится к предыдущему вопросу. Текущий ответ не изменён.",
            buttons=[[Button("К текущему вопросу ➡️", payload="a:resume")]] if emp.state.get("survey") else [],
        ),
    )
