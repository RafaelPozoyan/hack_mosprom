"""Агрегаты для дашборда HR и карточки сотрудника."""

from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Analysis, Employee, Question, SurveyAnswer, Ticket

STAGES = ["до выхода", "1-я неделя", "1-й месяц", "2-й месяц", "3-й месяц", "завершили"]


def stage(emp: Employee, today: date | None = None) -> str:
    today = today or date.today()
    if emp.status == "finished" or today > emp.probation_end:
        return "завершили"
    days = (today - emp.start_date).days
    if days < 0:
        return "до выхода"
    if days < 7:
        return "1-я неделя"
    return ("1-й месяц", "2-й месяц", "3-й месяц")[min(days // 30, 2)]


def mood_flag(topics: list[str]) -> str:
    """Причина без риск-флага простыми словами: тревожный ответ и его главная тема."""
    named = [t for t in topics if t != "другое"]
    return f"тревожное настроение: {named[0]}" if named else "тревожное настроение"


async def risk_signals(s: AsyncSession, since: datetime) -> dict[int, list[dict]]:
    """Сигналы риска по сотрудникам: явные тревожные ответы опросов + выводы NLP-анализа.
    source — откуда сигнал, для HR: «опрос» или «сообщение»."""
    out: dict[int, list[dict]] = defaultdict(list)
    for a in (
        await s.scalars(select(SurveyAnswer).where(SurveyAnswer.flagged, SurveyAnswer.created_at >= since))
    ).all():
        out[a.employee_id].append(
            {
                "flag": a.value.get("flag"),
                "source": "опрос",
                "text": a.comment or a.value.get("label", ""),
                "at": a.created_at.isoformat(),
            }
        )
    rows = await s.scalars(select(Analysis).where(Analysis.created_at >= since, Analysis.severity != "low"))
    for a in rows.all():
        for f in a.risk_flags or [mood_flag(a.topics)]:
            out[a.employee_id].append(
                {
                    "flag": f,
                    "source": "опрос" if a.source == "survey" else "сообщение",
                    "text": a.text[:300],
                    "severity": a.severity,
                    "at": a.created_at.isoformat(),
                }
            )
    return out


async def dashboard(s: AsyncSession) -> dict:
    now = datetime.now(ZoneInfo("UTC"))
    since = now - timedelta(days=30)
    emps = (await s.scalars(select(Employee))).all()
    by_stage = Counter(stage(e) for e in emps)
    questions = (await s.scalars(select(Question).where(Question.created_at >= since))).all()
    answered = sum(q.answered for q in questions)
    fb = [q.feedback for q in questions if q.feedback]
    open_tickets = await s.scalar(select(func.count()).select_from(Ticket).where(Ticket.status == "open"))
    red = await s.scalar(
        select(func.count()).select_from(Ticket).where(Ticket.status == "open", Ticket.severity == "high")
    )

    analyses = (
        await s.scalars(select(Analysis).where(Analysis.created_at >= now - timedelta(weeks=8)))
    ).all()
    weekly: dict[str, Counter] = defaultdict(Counter)
    for a in analyses:
        wk = (a.created_at.date() - timedelta(days=a.created_at.weekday())).isoformat()
        weekly[wk][a.sentiment] += 1
    topics = Counter(q.topic for q in questions if q.topic)
    gaps = Counter(q.text.strip().lower()[:120] for q in questions if not q.answered)

    scores: dict[str, list[int]] = defaultdict(list)
    for a in (await s.scalars(select(SurveyAnswer).where(SurveyAnswer.created_at >= since))).all():
        if "score" in a.value and a.value.get("topic"):
            scores[a.value["topic"]].append(a.value["score"])
    open_list = (
        await s.scalars(select(Ticket).where(Ticket.status == "open").order_by(Ticket.id.desc()).limit(6))
    ).all()
    return {
        "attention": [
            {
                "id": t.id,
                "kind": t.kind,
                "severity": t.severity,
                "employee": t.employee.full_name,
                "employee_id": t.employee_id,
                "subject": t.subject[:160],
                "created_at": t.created_at.isoformat(),
            }
            for t in open_list
        ],
        "kpi": {
            "newcomers": sum(by_stage[x] for x in STAGES[:5]),
            "questions": len(questions),
            "ai_resolved_pct": round(100 * answered / len(questions)) if questions else None,
            "helpful_pct": round(100 * sum(f > 0 for f in fb) / len(fb)) if fb else None,
            "open_tickets": open_tickets,
            "red_flags": red,
        },
        "stages": [{"stage": st, "count": by_stage.get(st, 0)} for st in STAGES],
        "sentiment_weekly": [
            {"week": wk, **{k: c.get(k, 0) for k in ("positive", "neutral", "negative")}}
            for wk, c in sorted(weekly.items())
        ],
        "question_topics": [{"topic": t, "count": c} for t, c in topics.most_common(8)],
        "knowledge_gaps": [{"question": t, "count": c} for t, c in gaps.most_common(10)],
        "scores": sorted(
            ({"topic": k, "avg": round(sum(v) / len(v), 1), "n": len(v)} for k, v in scores.items()),
            key=lambda x: x["avg"],
        ),
    }
