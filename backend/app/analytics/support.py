"""«Кому сегодня нужна поддержка» и подключение руководителя по решению HR.

Сигналы — те же, что в аналитике (metrics.risk_signals), с причиной простыми словами: на «Сегодня» — за сутки,
напротив человека в списке и в карточке — за месяц.
Рекомендацию руководителю HR выбирает из вариантов сценария (раздел manager_help): руководитель получает только
её текст — без ответов новичка и цитат. Если среди сигналов новичка есть флаг из analytics.never_to_manager,
сервер отказывает: даже рекомендация без подробностей может выдать руководителю, о чём рассказал новичок.
"""

from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.analytics import metrics
from app.bot.scenario import notify_audience, render, scenario
from app.models import Analysis, Employee, Message, SurveyAnswer

# «Сегодня» — последние сутки, а не календарный день: вечерний ответ не исчезает из блока утром.
# Настройка команды по запросу заказчика на чек-поинте 24.10: «где плохое настроение у кого сегодня».
TODAY = timedelta(days=1)
# В карточке — то же окно, что у списка сотрудников (admin.list_employees): причина видна одинаково в обоих местах.
RECENT = timedelta(days=30)

PRIVATE = "Сигнал касается руководителя — обсудите с HR-руководителем"
NO_MANAGER = "Руководитель не назначен — рекомендацию некому отправить"


class NoManager(Exception):
    pass


class PrivateSignal(Exception):
    pass


def rank(r: dict) -> tuple[bool, datetime]:
    """Порядок показа: срочные первыми, затем самые свежие."""
    return r["urgent"], datetime.fromisoformat(r["at"])


def reasons(signals: list[dict]) -> list[dict]:
    """Причины для HR: одна строка на причину и источник, без ответа новичка.
    Срочность — как у реакции на сигнал (sentiment.react): высокая важность или флаг из analytics.high."""
    high = set(scenario()["analytics"]["high"])
    out: dict[tuple[str, str], dict] = {}
    for x in signals:
        reason = x["flag"] or "тревожный ответ"
        new = {
            "reason": reason,
            "source": x["source"],
            "at": x["at"],
            "urgent": x.get("severity") == "high" or reason in high,
        }
        r = out.setdefault((reason, x["source"]), new)
        r["urgent"] = r["urgent"] or new["urgent"]
        r["at"] = max(r["at"], x["at"], key=datetime.fromisoformat)
    return sorted(out.values(), key=rank, reverse=True)


def top_reason(signals: list[dict]) -> dict | None:
    """Главная причина — то, что видно напротив человека в списке сотрудников."""
    return next(iter(reasons(signals)), None)


async def today(s: AsyncSession) -> list[dict]:
    """Новички с тревожными сигналами за последние сутки."""
    signals = await metrics.risk_signals(s, datetime.now(UTC) - TODAY)
    people = []
    for e in (await s.scalars(select(Employee).where(Employee.id.in_(list(signals))))).all():
        rs = reasons(signals[e.id])
        people.append({"employee_id": e.id, "name": e.full_name, "urgent": rs[0]["urgent"], "reasons": rs})
    return sorted(people, key=lambda p: rank(p["reasons"][0]), reverse=True)


async def private_flags(s: AsyncSession, emp_id: int) -> list[str]:
    """Флаги never_to_manager за всю адаптацию: из ответов опросов и из разбора свободных ответов любой важности."""
    flagged = select(SurveyAnswer.value).where(SurveyAnswer.employee_id == emp_id, SurveyAnswer.flagged)
    flags = {v.get("flag") for v in await s.scalars(flagged)}
    for row in await s.scalars(select(Analysis.risk_flags).where(Analysis.employee_id == emp_id)):
        flags.update(row or [])
    return sorted(flags & set(scenario()["analytics"]["never_to_manager"]))


def options(emp: Employee) -> list[dict]:
    """Варианты рекомендации с готовым текстом — ровно то, что получит руководитель."""
    cfg = scenario()["manager_help"]
    return [
        {"id": o["id"], "title": o["title"], "text": render(f"{cfg['intro']}\n\n{o['text']}", emp)}
        for o in cfg["options"]
    ]


async def card(s: AsyncSession, emp: Employee) -> dict:
    """Для карточки новичка: причины за месяц (today — была ли за сутки) и можно ли подключить руководителя
    (refusal — почему нельзя)."""
    now = datetime.now(UTC)
    rs = reasons((await metrics.risk_signals(s, now - RECENT)).get(emp.id, []))
    refusal = NO_MANAGER if emp.manager is None else (PRIVATE if await private_flags(s, emp.id) else None)
    return {
        "reasons": rs,
        "today": any(datetime.fromisoformat(r["at"]) >= now - TODAY for r in rs),
        "manager": emp.manager.name if emp.manager else None,
        "manager_in_max": bool(emp.manager and emp.manager.max_user_id),
        "refusal": refusal,
        "options": options(emp),
    }


async def connect_manager(s: AsyncSession, emp: Employee, option: dict) -> Message:
    """Рекомендация HR руководителю: в MAX, если он подключён, и всегда — на его экране и в карточке новичка.
    Проверки здесь, на сервере: интерфейс только повторяет их, а прямой вызов API их не обходит."""
    if emp.manager is None:
        raise NoManager(NO_MANAGER)
    if flags := await private_flags(s, emp.id):
        raise PrivateSignal(f"{PRIVATE}. Руководителю не передаётся: {', '.join(flags)}.")
    return await notify_audience(s, emp, "manager", option["text"], None, request=option["title"])
