"""Еженедельный дайджест «Настроения в коллективе новичков» для руководителя управления по подбору (роль 04).

Свободные ответы группируются по темам и рискам таксономии (кластеры из кейса: «проблемы с доступами»,
«конфликт с наставником»), цифры считаются детерминированно в SQL, LLM только пишет связный текст по ним.
"""

import logging
from collections import Counter
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import channels, llm
from app.channels import OutMessage
from app.models import Analysis, Digest, Employee, Question, Staff, SurveyAnswer, Ticket

log = logging.getLogger(__name__)


async def collect(s: AsyncSession, start: date, end: date) -> dict:
    t0 = datetime.combine(start, datetime.min.time(), tzinfo=ZoneInfo("UTC"))
    t1 = datetime.combine(end + timedelta(days=1), datetime.min.time(), tzinfo=ZoneInfo("UTC"))
    analyses = (await s.scalars(select(Analysis).where(Analysis.created_at >= t0, Analysis.created_at < t1))).all()
    answers = (await s.scalars(select(SurveyAnswer).where(SurveyAnswer.created_at >= t0, SurveyAnswer.created_at < t1))).all()
    questions = (await s.scalars(select(Question).where(Question.created_at >= t0, Question.created_at < t1))).all()
    tickets = (await s.scalars(select(Ticket).where(Ticket.created_at >= t0, Ticket.created_at < t1))).all()
    active = (await s.scalars(select(Employee).where(Employee.status == "active"))).all()

    sentiment = Counter(a.sentiment for a in analyses)
    clusters: dict[str, dict] = {}
    for a in analyses:
        for key in a.risk_flags or [t for t in a.topics if a.sentiment == "negative"]:
            c = clusters.setdefault(key, {"count": 0, "employees": set(), "quotes": []})
            c["count"] += 1
            c["employees"].add(a.employee_id)
            if len(c["quotes"]) < 3:
                c["quotes"].append(a.text[:200])
    for a in answers:
        if a.flagged and a.value.get("flag"):
            c = clusters.setdefault(a.value["flag"], {"count": 0, "employees": set(), "quotes": []})
            c["count"] += 1
            c["employees"].add(a.employee_id)
    scores: dict[str, list[int]] = {}
    for a in answers:
        if "score" in a.value and a.value.get("topic"):
            scores.setdefault(a.value["topic"], []).append(a.value["score"])
    return {
        "period": f"{start:%d.%m.%Y}–{end:%d.%m.%Y}",
        "active_newcomers": len(active),
        "texts_analyzed": len(analyses),
        "sentiment": dict(sentiment),
        "clusters": sorted(
            ({"name": k, "count": v["count"], "employees": len(v["employees"]), "quotes": v["quotes"]} for k, v in clusters.items()),
            key=lambda c: c["count"],
            reverse=True,
        ),
        "avg_scores": {k: round(sum(v) / len(v), 1) for k, v in scores.items()},
        "questions": len(questions),
        "answered_by_ai": sum(q.answered for q in questions),
        "escalations": len(tickets),
        "red_flags": sum(t.severity == "high" for t in tickets),
    }


def render_stats(st: dict) -> str:
    lines = [
        f"📊 Настроения в коллективе новичков · {st['period']}",
        f"Новичков в адаптации: {st['active_newcomers']} · проанализировано ответов: {st['texts_analyzed']}",
        f"Вопросов к Корсику: {st['questions']}, из них ИИ ответил сам: {st['answered_by_ai']}",
        f"Эскалаций: {st['escalations']}, красных флагов: {st['red_flags']}",
    ]
    if st["clusters"]:
        lines.append("\nЗоны внимания:")
        lines += [f"• {c['name']} — {c['count']} сигн., {c['employees']} чел." for c in st["clusters"][:6]]
    if st["avg_scores"]:
        lines.append("\nСредние оценки (1–10):")
        lines += [f"• {k}: {v}" for k, v in sorted(st["avg_scores"].items(), key=lambda x: x[1])]
    return "\n".join(lines)


async def narrative(st: dict) -> str:
    try:
        return await llm.complete(
            [
                {
                    "role": "system",
                    "content": "Ты HR-аналитик. По статистике пульс-опросов новичков напиши 3–5 предложений для руководителя "
                    "управления по подбору: главные зоны риска, на что обратить внимание, 1–2 конкретных шага. "
                    "Используй только факты и числа из данных, ничего не добавляй. Без приветствий.",
                },
                {"role": "user", "content": str(st)},
            ]
        )
    except Exception:
        log.exception("digest narrative failed")
        return ""


async def run(s: AsyncSession, end: date | None = None) -> Digest:
    end = end or date.today()
    start = end - timedelta(days=6)
    st = await collect(s, start, end)
    text = render_stats(st)
    story = await narrative(st)
    if story:
        text += "\n\n🧠 " + story.strip()
    d = Digest(period_start=start, period_end=end, stats=st, text=text)
    s.add(d)
    await s.commit()
    for head in (await s.scalars(select(Staff).where(Staff.role == "head"))).all():
        await channels.notify_staff(head, OutMessage(text=text))
    return d
