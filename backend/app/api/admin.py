"""REST API панели HR: сотрудники, таймлайн, эскалации, база знаний, аналитика."""

import asyncio
import io
import json
import secrets
from datetime import date
from pathlib import Path

import segno
from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Query, UploadFile
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import scheduler
from app.analytics import digest, metrics
from app.analytics.sentiment import manager_topics
from app.bot import escalation, progress
from app.bot.progress import StepBlocked
from app.bot.scenario import (
    add_months,
    button_rows,
    phase_of,
    reload_scenario,
    render,
    save_step_texts,
    scenario,
    schedule_employee,
    step,
    timeline,
)
from app.channels import ADMIN_FEED, hub, message_view
from app.config import settings
from app.db import get_session, session
from app.models import (
    Analysis,
    Chunk,
    Digest,
    Document,
    Employee,
    Job,
    Message,
    Question,
    Site,
    Staff,
    StepAttachment,
    SurveyAnswer,
    Ticket,
)
from app.rag import ingest
from app.rag.answer import answer as rag_answer
from app.rag.search import search


def auth(authorization: str | None = Header(None), token: str | None = Query(None)) -> None:
    expected = settings().admin_token
    if expected and authorization != f"Bearer {expected}" and token != expected:
        raise HTTPException(401, "unauthorized")


router = APIRouter(prefix="/api", dependencies=[Depends(auth)])


# --- справочники -------------------------------------------------------------------------------------


@router.get("/meta")
async def meta(s: AsyncSession = Depends(get_session)):
    sites = (await s.scalars(select(Site))).all()
    staff = (await s.scalars(select(Staff))).all()
    steps = []
    seen = set()
    for st in scenario()["steps"]:
        key = (st["id"], tuple(st.get("sites", [])))
        if key in seen:
            continue
        seen.add(key)
        steps.append({"id": st["id"], "title": st.get("title", st["id"]), "phase": phase_of(st.get("at")), "at": st.get("at"),
                      "sites": st.get("sites", []),
                      "manual": st.get("manual", False), "audience": st.get("audience", "employee"),
                      "preview": (st.get("messages") or [{}])[0].get("text", "")[:160]})
    cfg = settings()
    return {
        "sites": [{"code": x.code, "name": x.name, "kind": x.kind, "tz": x.tz} for x in sites],
        "staff": [staff_view(x) for x in staff],
        "steps": steps,
        "surveys": {k: {"title": v["title"], "questions": len(v["questions"])} for k, v in scenario()["surveys"].items()},
        "max_connected": bool(cfg.max_token),
        "max_bot_name": cfg.max_bot_name,
        "llm_model": cfg.llm_model,
    }


def staff_view(x: Staff) -> dict:
    return {"id": x.id, "name": x.name, "role": x.role, "site_code": x.site_code, "max_connected": bool(x.max_user_id),
            "max_link": x.max_link, "invite_link": deep_link(f"staff_{x.invite_code}"),
            # личная ссылка руководителя на его экран (/m/<код>); код видит только HR — /api/meta закрыт токеном панели
            "code": x.invite_code if x.role == "manager" else None}


def deep_link(payload: str) -> str | None:
    bot = settings().max_bot_name
    return f"https://max.ru/{bot}?start={payload}" if bot else None


class StaffIn(BaseModel):
    name: str
    role: str
    site_code: str | None = None
    max_link: str | None = None


@router.post("/staff")
async def create_staff(body: StaffIn, s: AsyncSession = Depends(get_session)):
    x = Staff(**body.model_dump(), invite_code=secrets.token_urlsafe(9))
    s.add(x)
    await s.commit()
    return staff_view(x)


# --- сотрудники --------------------------------------------------------------------------------------


class EmployeeIn(BaseModel):
    full_name: str
    first_name: str | None = None
    position: str = ""
    department: str = ""
    site_code: str
    start_date: date
    probation_end: date | None = None
    manager_id: int | None = None
    recruiter_id: int | None = None


def employee_view(e: Employee) -> dict:
    return {
        "id": e.id, "full_name": e.full_name, "first_name": e.first_name, "position": e.position, "department": e.department,
        "site_code": e.site_code, "start_date": e.start_date.isoformat(), "probation_end": e.probation_end.isoformat(),
        "manager": e.manager.name if e.manager else None, "manager_id": e.manager_id,
        "recruiter": e.recruiter.name if e.recruiter else None, "recruiter_id": e.recruiter_id,
        "address_form": e.address_form, "channel": e.channel, "status": e.status, "stage": metrics.stage(e),
        "invite_code": e.invite_code, "invite_link": deep_link(e.invite_code),
        "survey": (e.state or {}).get("survey"), "in_dialog_with_hr": bool((e.state or {}).get("ticket_id")),
    }


@router.get("/employees")
async def list_employees(s: AsyncSession = Depends(get_session)):
    emps = (await s.scalars(select(Employee).order_by(Employee.start_date.desc()))).all()
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    signals = await metrics.risk_signals(s, datetime.now(ZoneInfo("UTC")) - timedelta(days=30))
    open_t = {r for r in await s.scalars(select(Ticket.employee_id).where(Ticket.status == "open"))}
    return [{**employee_view(e), "signals": len(signals.get(e.id, [])), "open_ticket": e.id in open_t} for e in emps]


@router.post("/employees")
async def create_employee(body: EmployeeIn, s: AsyncSession = Depends(get_session)):
    data = body.model_dump()
    data["first_name"] = data["first_name"] or body.full_name.split()[1 if len(body.full_name.split()) > 1 else 0]
    data["probation_end"] = data["probation_end"] or add_months(body.start_date, settings().probation_months)
    e = Employee(**data, invite_code=secrets.token_urlsafe(9), state={})
    s.add(e)
    await s.commit()
    s.expunge(e)  # перечитываем вместе со связями (руководитель, рекрутер, площадка)
    e = await s.get(Employee, e.id)
    await schedule_employee(s, e)
    return employee_view(e)


@router.patch("/employees/{emp_id}")
async def update_employee(emp_id: int, body: dict, s: AsyncSession = Depends(get_session)):
    e = await _emp(s, emp_id)
    for k in ("full_name", "first_name", "position", "department", "site_code", "manager_id", "recruiter_id"):
        if k in body:
            setattr(e, k, body[k])
    for k in ("start_date", "probation_end"):
        if body.get(k):
            setattr(e, k, date.fromisoformat(body[k]))
    await s.commit()
    s.expunge(e)
    e = await _emp(s, emp_id)
    await schedule_employee(s, e)
    return employee_view(e)


@router.delete("/employees/{emp_id}")
async def delete_employee(emp_id: int, s: AsyncSession = Depends(get_session)):
    await s.execute(delete(Employee).where(Employee.id == emp_id))
    await s.commit()
    return {"ok": True}


async def _emp(s: AsyncSession, emp_id: int) -> Employee:
    e = await s.get(Employee, emp_id)
    if not e:
        raise HTTPException(404, "employee not found")
    return e


@router.get("/employees/{emp_id}")
async def employee_card(emp_id: int, s: AsyncSession = Depends(get_session)):
    e = await _emp(s, emp_id)
    jobs = (await s.scalars(select(Job).where(Job.employee_id == emp_id, Job.kind == "step"))).all()
    job_by_step = {}
    for j in sorted(jobs, key=lambda j: j.id):
        job_by_step[j.ref] = j
    sent_steps = set(await s.scalars(select(Message.step_id).where(Message.employee_id == emp_id, Message.step_id.is_not(None))))
    tl = []
    for st, run_at in timeline(e):
        j = job_by_step.get(st["id"])
        status = "sent" if st["id"] in sent_steps else (j.status if j else ("manual" if st.get("manual") else "skipped"))
        first = (st.get("messages") or [{}])[0]
        tl.append({"step_id": st["id"], "title": st.get("title", st["id"]), "phase": phase_of(st.get("at")),
                   "run_at": run_at.isoformat() if run_at else None,
                   "manual": st.get("manual", False), "audience": st.get("audience", "employee"), "status": status,
                   "preview": render(first.get("text") or first.get("ai") or "", e)[:160]})
    answers = (await s.scalars(select(SurveyAnswer).where(SurveyAnswer.employee_id == emp_id).order_by(SurveyAnswer.id))).all()
    survey_groups = answers_view(e, answers)
    analyses = (await s.scalars(select(Analysis).where(Analysis.employee_id == emp_id).order_by(Analysis.id.desc()))).all()
    tickets = (await s.scalars(select(Ticket).where(Ticket.employee_id == emp_id).order_by(Ticket.id.desc()))).all()
    notes = (await s.scalars(select(Message).where(Message.employee_id == emp_id, Message.author.like("to_%")).order_by(Message.id.desc()))).all()
    return {
        "employee": employee_view(e),
        "timeline": sorted(tl, key=lambda x: x["run_at"] or ""),
        "surveys": survey_groups,
        "analyses": [{"text": a.text, "sentiment": a.sentiment, "emotions": a.emotions, "topics": a.topics, "risk_flags": a.risk_flags,
                      "severity": a.severity, "summary": a.summary, "source": a.source, "at": a.created_at.isoformat()} for a in analyses],
        "tickets": [ticket_view(t) for t in tickets],
        "notifications": [{"to": m.author[3:], "text": m.text, "delivered": m.payload.get("delivered"), "at": m.created_at.isoformat()} for m in notes],
    }


def answer_label(v: dict) -> str:
    if v.get("label"):
        return v["label"]
    if "score" in v:
        return f"{v['score']} из 10"
    if v.get("answer") in ("yes", "no"):
        return "Да" if v["answer"] == "yes" else "Нет"
    if v.get("labels") is not None:
        return ", ".join(v["labels"]) or "—"
    return v.get("text") or "пропущено"


def answers_view(e: Employee, answers: list[SurveyAnswer]) -> list[dict]:
    """Ответы с формулировками вопросов, сгруппированные по опросам, — то, что HR читает без расшифровки."""
    groups: dict[str, dict] = {}
    for a in answers:
        q: dict = {}
        if a.survey_id.startswith("step:"):
            st = step(a.question_id, e.site_code) or {}
            title, question = "Ответы по ходу адаптации", st.get("title", a.question_id)
        else:
            sd = scenario()["surveys"].get(a.survey_id, {})
            q = next((x for x in sd.get("questions", []) if x["id"] == a.question_id), {})
            title, question = sd.get("title", a.survey_id), render(q.get("text", a.question_id), e)
        v = a.value or {}
        concern = a.flagged or ("score" in v and v["score"] < q.get("comment_below", 0)) or (
            q.get("type") == "yesno" and v.get("answer") == q.get("comment_on"))
        g = groups.setdefault(title, {"title": title, "at": a.created_at.isoformat(), "items": []})
        g["items"].append({"question": question, "answer": answer_label(v), "comment": a.comment, "concern": bool(concern),
                           "flag": v.get("flag")})
    return list(groups.values())


# --- экран руководителя -------------------------------------------------------------------------------
# Отдельный роутер без токена HR: руководитель открывает личную ссылку /m/<код>. Сервер отдаёт только его
# сотрудников и только поля из MANAGER_FIELDS — свободные ответы, опросы, обращения и коды приглашений сюда не попадают.
# Роль «Руководитель» в панели HR показывает этот же ответ сервера.

manager_router = APIRouter(prefix="/api/m")
MANAGER_FIELDS = ("id", "full_name", "first_name", "position", "department", "site_code", "start_date", "probation_end",
                  "status", "stage")


def manager_view(e: Employee) -> dict:
    v = employee_view(e)
    return {k: v[k] for k in MANAGER_FIELDS}


async def _manager(s: AsyncSession, code: str) -> Staff:
    m = await s.scalar(select(Staff).where(Staff.invite_code == code, Staff.role == "manager"))
    if not m:
        raise HTTPException(404, "ссылка недействительна")
    return m


@manager_router.get("/{code}")
async def manager_home(code: str, s: AsyncSession = Depends(get_session)):
    m = await _manager(s, code)
    emps = (await s.scalars(select(Employee).where(Employee.manager_id == m.id).order_by(Employee.start_date.desc()))).all()
    return {"manager": m.name, "employees": [manager_view(e) for e in emps]}


@manager_router.get("/{code}/employees/{emp_id}")
async def manager_card(code: str, emp_id: int, s: AsyncSession = Depends(get_session)):
    """Прогресс и рекомендации по своему новичку — по той же политике, что push руководителю."""
    m = await _manager(s, code)
    e = await s.get(Employee, emp_id)
    if not e or e.manager_id != m.id:
        raise HTTPException(404, "сотрудник не найден")
    from datetime import UTC, datetime, timedelta

    since = datetime.now(UTC) - timedelta(days=90)
    flagged = (await s.scalars(select(SurveyAnswer).where(SurveyAnswer.employee_id == emp_id, SurveyAnswer.flagged,
                                                          SurveyAnswer.created_at >= since))).all()
    analyses = (await s.scalars(select(Analysis).where(Analysis.employee_id == emp_id, Analysis.created_at >= since,
                                                       Analysis.severity != "low"))).all()
    topics = manager_topics([[a.value.get("flag")] for a in flagged] + [a.risk_flags or [] for a in analyses],
                            scenario()["analytics"])
    upcoming = [
        {"title": st.get("title", st["id"]), "run_at": run_at.isoformat()}
        for st, run_at in timeline(e)
        if run_at and st.get("audience", "employee") == "employee" and run_at > datetime.now(UTC)
    ][:4]
    notes = (await s.scalars(select(Message).where(Message.employee_id == emp_id, Message.author == "to_manager").order_by(Message.id.desc()))).all()
    return {
        "employee": manager_view(e),
        "signals": topics,
        "upcoming": upcoming,
        "notifications": [{"text": n.text, "delivered": n.payload.get("delivered"), "at": n.created_at.isoformat()} for n in notes],
    }


@router.get("/employees/{emp_id}/qr.svg")
async def employee_qr(emp_id: int, s: AsyncSession = Depends(get_session)):
    e = await _emp(s, emp_id)
    link = deep_link(e.invite_code) or f"{settings().public_base_url}/demo/{e.id}"
    buf = io.BytesIO()
    segno.make(link, error="m").save(buf, kind="svg", scale=6, dark="#0b3a4a")
    return Response(buf.getvalue(), media_type="image/svg+xml")


@router.post("/employees/{emp_id}/steps/{step_id}/run")
async def run_step_now(emp_id: int, step_id: str, file: UploadFile | None = File(None), s: AsyncSession = Depends(get_session)):
    """Запуск шага вручную: «по отмашке рекрутера» (ТЗ) или перемотка таймлайна в демо."""
    e = await _emp(s, emp_id)
    if step(step_id, e.site_code) is None:
        raise HTTPException(404, "Шаг для этой площадки не найден")
    # Не ждём свободного исполнителя: задержанный двойной клик нельзя считать намеренным повтором.
    if scheduler._job_slots.locked():
        raise HTTPException(409, "Сейчас выполняются другие отправки. Подождите и повторите.")
    async with (scheduler._job_slots, scheduler.session() as guard, guard.begin(),
                scheduler.employee_owner(guard, emp_id) as owned):
        if not owned:
            raise HTTPException(409, "Сейчас уже отправляется другой шаг. Дождитесь завершения и повторите.")
        await s.refresh(e)  # диалог мог изменить состояние, пока ожидали локальную блокировку
        current_step = step(step_id, e.site_code)
        if current_step is None:
            raise HTTPException(404, "Шаг для этой площадки не найден")
        try:
            progress.check_step(e, current_step)
        except StepBlocked as exc:
            raise HTTPException(409, str(exc)) from exc
        j = await s.scalar(select(Job).where(
            Job.employee_id == emp_id, Job.kind == "step", Job.ref == step_id,
            Job.status.in_(["pending", "running", "blocked"]),
        ).order_by(Job.run_at, Job.id))
        if j:
            if not await guard.scalar(select(func.pg_try_advisory_xact_lock(scheduler.JOB_LOCK_NAMESPACE, j.id))):
                raise HTTPException(409, "Этот шаг уже запускается. Дождитесь завершения и повторите.")
            await s.refresh(j)
            if j.status == "running" or j.attempts:
                raise HTTPException(409, "Предыдущая отправка ещё обрабатывается. Проверьте её состояние перед повтором.")
        else:
            j = Job(employee_id=emp_id, kind="step", ref=step_id, run_at=scheduler.utcnow())
            s.add(j)
        await s.flush()
        if file is not None:
            name = Path((file.filename or "Вложение").replace("\\", "/")).name or "Вложение"
            filename = f"upload_{secrets.token_hex(16)}{Path(name).suffix[:16]}"
            path = settings().media_dir / filename
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(await file.read())
            s.add(StepAttachment(job_id=j.id, file=filename, name=name))
        # Файл остаётся на диске и при неопределённом результате commit.
        j.status, j.attempts = "running", 1
        await s.commit()
        await scheduler._execute(s, j)
        if j.status == "blocked":
            raise HTTPException(409, j.error)
        if j.status != "done":
            raise HTTPException(503, "Отправка не завершена. Задача сохранена для автоматического повтора; проверьте её состояние позже.")
    return {"ok": True}


# --- эскалации ---------------------------------------------------------------------------------------


def ticket_view(t: Ticket) -> dict:
    return {"id": t.id, "kind": t.kind, "status": t.status, "severity": t.severity, "subject": t.subject, "summary": t.summary,
            "employee_id": t.employee_id, "employee": t.employee.full_name, "site": t.employee.site_code,
            "created_at": t.created_at.isoformat(), "closed_at": t.closed_at.isoformat() if t.closed_at else None}


@router.get("/tickets")
async def list_tickets(status: str | None = None, s: AsyncSession = Depends(get_session)):
    q = select(Ticket).order_by(Ticket.id.desc())
    if status:
        q = q.where(Ticket.status == status)
    return [ticket_view(t) for t in (await s.scalars(q)).all()]


async def _ticket(s: AsyncSession, tid: int) -> Ticket:
    t = await s.get(Ticket, tid)
    if not t:
        raise HTTPException(404, "Обращение не найдено")
    return t


@router.get("/tickets/{tid}")
async def get_ticket(tid: int, s: AsyncSession = Depends(get_session)):
    t = await _ticket(s, tid)
    msgs = (await s.scalars(
        select(Message).where(Message.employee_id == t.employee_id, Message.author.in_(["employee", "bot", "hr"]),
                              Message.created_at >= t.created_at).order_by(Message.id)
    )).all()
    before = (await s.scalars(
        select(Message).where(Message.employee_id == t.employee_id, Message.author.in_(["employee", "bot", "hr"]),
                              Message.created_at < t.created_at).order_by(Message.id.desc()).limit(8)
    )).all()
    return {**ticket_view(t), "messages": [message_view(m) for m in [*reversed(before), *msgs]]}


class ReplyIn(BaseModel):
    text: str
    author: str = "HR"


@router.post("/tickets/{tid}/reply")
async def reply_ticket(tid: int, body: ReplyIn, s: AsyncSession = Depends(get_session)):
    t = await _ticket(s, tid)
    try:
        await escalation.hr_reply(s, t, body.text, body.author)
    except escalation.TicketClosedError as e:
        raise HTTPException(409, str(e)) from e
    return {"ok": True}


@router.post("/tickets/{tid}/close")
async def close_ticket(tid: int, s: AsyncSession = Depends(get_session)):
    t = await _ticket(s, tid)
    await escalation.close(s, t, notify=t.kind != "alert")
    return {"ok": True}


# --- база знаний -------------------------------------------------------------------------------------


def doc_view(d: Document) -> dict:
    return {"id": d.id, "title": d.title, "filename": d.filename, "site_codes": d.site_codes, "status": d.status,
            "error": d.error, "n_chunks": d.n_chunks, "created_at": d.created_at.isoformat()}


@router.get("/documents")
async def list_documents(s: AsyncSession = Depends(get_session)):
    return [doc_view(d) for d in (await s.scalars(select(Document).order_by(Document.id.desc()))).all()]


@router.post("/documents")
async def upload_document(file: UploadFile = File(...), title: str = Form(""), sites: str = Form("")):
    data = await file.read()
    site_codes = [x for x in sites.split(",") if x]

    async def job():
        async with session() as s:
            await ingest.ingest(s, file.filename, data, title or file.filename.rsplit(".", 1)[0], site_codes, settings().chunk_max_chars)

    asyncio.create_task(job())
    return {"ok": True}


@router.delete("/documents/{doc_id}")
async def delete_document(doc_id: int, s: AsyncSession = Depends(get_session)):
    await s.execute(delete(Document).where(Document.id == doc_id))
    await s.commit()
    return {"ok": True}


@router.get("/documents/{doc_id}/chunks")
async def document_chunks(doc_id: int, s: AsyncSession = Depends(get_session)):
    rows = (await s.scalars(select(Chunk).where(Chunk.document_id == doc_id).order_by(Chunk.ord))).all()
    return [{"id": c.id, "heading": c.heading, "text": c.text} for c in rows]


class DebugIn(BaseModel):
    question: str
    site: str = ""


@router.post("/rag/debug")
async def rag_debug(body: DebugIn, s: AsyncSession = Depends(get_session)):
    """Песочница: что нашёл поиск, с какими оценками, и что ответил бы бот."""
    hits = await search(s, body.question, body.site, top_n=10)
    res = await rag_answer(s, body.question, body.site)
    return {
        "hits": [{"chunk_id": h.chunk_id, "title": h.title, "heading": h.heading, "text": h.text[:600], "score": round(h.score, 4)} for h in hits],
        "answered": res.answered, "answer": res.text, "reason": res.reason, "sources": res.sources,
        "threshold": settings().rerank_min_score,
    }


@router.get("/questions")
async def list_questions(answered: bool | None = None, s: AsyncSession = Depends(get_session)):
    q = select(Question).order_by(Question.id.desc()).limit(200)
    if answered is not None:
        q = q.where(Question.answered == answered)
    return [{"id": x.id, "text": x.text, "answered": x.answered, "reason": x.reason, "score": x.best_score, "topic": x.topic,
             "feedback": x.feedback, "at": x.created_at.isoformat()} for x in (await s.scalars(q)).all()]


# --- аналитика ---------------------------------------------------------------------------------------


@router.get("/dashboard")
async def dashboard(s: AsyncSession = Depends(get_session)):
    return await metrics.dashboard(s)


@router.get("/digests")
async def list_digests(s: AsyncSession = Depends(get_session)):
    rows = (await s.scalars(select(Digest).order_by(Digest.id.desc()).limit(10))).all()
    return [{"id": d.id, "period": d.stats.get("period"), "text": d.text, "stats": d.stats, "created_at": d.created_at.isoformat()} for d in rows]


@router.post("/digests/run")
async def run_digest(s: AsyncSession = Depends(get_session)):
    d = await digest.run(s)
    return {"id": d.id, "text": d.text}


class StepTextsIn(BaseModel):
    sites: list[str] = []
    texts: dict[int, str]


@router.get("/scenario/steps/{step_id}")
async def get_step(step_id: str, sites: str = ""):
    want = sorted(x for x in sites.split(",") if x)
    for st in scenario()["steps"]:
        if st["id"] == step_id and sorted(st.get("sites", [])) == want:
            return {"id": st["id"], "title": st.get("title", st["id"]), "sites": st.get("sites", []),
                    "messages": [{"index": i, "text": m.get("text"), "ai": m.get("ai"), "survey": m.get("survey"),
                                  "image": m.get("image"), "buttons": [b.get("text") for row in button_rows(m.get("buttons")) for b in row]}
                                 for i, m in enumerate(st.get("messages", []))]}
    raise HTTPException(404)


@router.put("/scenario/steps/{step_id}")
async def update_step(step_id: str, body: StepTextsIn):
    """Правка текстов шага из панели — без программиста; изменения сразу видны в следующих сообщениях."""
    save_step_texts(step_id, body.sites, body.texts)
    return {"ok": True}


@router.post("/scenario/reload")
async def scenario_reload(s: AsyncSession = Depends(get_session)):
    reload_scenario()
    for e in (await s.scalars(select(Employee).where(Employee.status != "finished"))).all():
        await schedule_employee(s, e)
    return {"ok": True}


@router.get("/feed")
async def feed():
    """SSE: новые эскалации и сообщения в них — для живого обновления панели HR."""
    q = hub.subscribe(ADMIN_FEED)

    async def stream():
        try:
            yield ": connected\n\n"
            while True:
                try:
                    ev = await asyncio.wait_for(q.get(), timeout=25)
                    yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
                except TimeoutError:
                    yield ": ping\n\n"
        finally:
            hub.unsubscribe(ADMIN_FEED, q)

    return StreamingResponse(stream(), media_type="text/event-stream")
