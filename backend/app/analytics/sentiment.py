"""NLP-анализ свободных ответов сотрудников: тональность, эмоции, темы, риск-флаги + реакция по политике из YAML.

Подход — аспектный анализ через LLM со строгой схемой ответа: в отличие от бинарного классификатора
тональности он объясняет, *что* именно не так («проблемы с доступами»), и это сразу можно передать HR.
"""

from typing import Literal

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app import channels, llm
from app.channels import Button, OutMessage
from app.models import Analysis, Employee, Message


def taxonomy() -> dict:
    from app.bot.scenario import scenario

    return scenario()["analytics"]


class TextAnalysis(BaseModel):
    sentiment: Literal["negative", "neutral", "positive"]
    emotions: list[str]
    topics: list[str]
    risk_flags: list[str]
    severity: Literal["low", "medium", "high"]
    summary: str


SYSTEM = """Ты — аналитик HR-службы. Анализируешь короткий текст нового сотрудника (ответ в пульс-опросе или сообщение боту адаптации).
Верни:
- sentiment: тональность текста;
- emotions: эмоции только из списка {emotions} (пусто, если эмоций нет);
- topics: темы только из списка {topics};
- risk_flags: риски только из списка {risks}; ставь флаг, только если текст прямо на него указывает;
- severity: high — нужна реакция HR в течение дня (конфликт, токсичность, выгорание, намерение уйти, дискриминация, угроза здоровью);
  medium — есть проблема, но не срочно; low — проблем нет;
- summary: одно нейтральное предложение для HR, без домыслов и без оценок личности.
Не придумывай то, чего нет в тексте."""


async def analyze_text(text: str, context: str = "") -> TextAnalysis:
    tx = taxonomy()
    system = SYSTEM.format(
        emotions=", ".join(tx["emotions"]), topics=", ".join(tx["topics"]), risks=", ".join(tx["risk_flags"])
    )
    user = f"Контекст: {context}\n\nТекст сотрудника: «{text}»" if context else f"Текст сотрудника: «{text}»"
    res = await llm.structured(
        [{"role": "system", "content": system}, {"role": "user", "content": user}], TextAnalysis
    )
    # модель могла вернуть значение вне таксономии — отбрасываем, чтобы аналитика оставалась сопоставимой
    res.emotions = [e for e in res.emotions if e in tx["emotions"]]
    res.topics = [t for t in res.topics if t in tx["topics"]] or ["другое"]
    res.risk_flags = [f for f in res.risk_flags if f in tx["risk_flags"]]
    return res


async def last_bot_text(s: AsyncSession, emp: Employee) -> str:
    from sqlalchemy import select

    m = await s.scalar(
        select(Message)
        .where(Message.employee_id == emp.id, Message.author == "bot")
        .order_by(Message.id.desc())
        .limit(1)
    )
    return m.text[:500] if m else ""


async def save_and_react(
    s: AsyncSession,
    emp: Employee,
    res: TextAnalysis | None,
    text: str,
    source: str,
    extra_flags: list[str] | None = None,
) -> Analysis | None:
    """res — разбор LLM (None, если он не удался); extra_flags — явный сигнал из выбранного варианта ответа
    (например, «Нет, задумываюсь об уходе»): он срабатывает и без LLM."""
    if res is None:
        if extra_flags:
            await react(s, emp, extra_flags, "medium", text)
        return None
    res.risk_flags = sorted(set(res.risk_flags) | set(extra_flags or []))
    a = Analysis(
        employee_id=emp.id,
        source=source,
        text=text,
        sentiment=res.sentiment,
        emotions=res.emotions,
        topics=res.topics,
        risk_flags=res.risk_flags,
        severity=res.severity,
        summary=res.summary,
    )
    s.add(a)
    await s.commit()
    await react(s, emp, res.risk_flags, res.severity, text, offer_employee=source == "chat")
    return a


def manager_topics(flag_sets: list[list[str]], tx: dict) -> list[str]:
    """Что можно показать руководителю: только сигналы из manager_actionable, и ничего из записи,
    где есть сигнал never_to_manager (например, конфликт с руководителем), — даже соседние темы этой записи."""
    never, allowed = set(tx["never_to_manager"]), set(tx["manager_actionable"])
    return sorted({f for flags in flag_sets if not never & set(flags) for f in flags if f in allowed})


async def react(
    s: AsyncSession, emp: Employee, flags: list[str], severity: str, text: str, offer_employee: bool = False
) -> None:
    """Реакция на сигналы — и из LLM-анализа, и из «тревожных» вариантов ответов опроса."""
    from app.bot.escalation import create_ticket
    from app.bot.scenario import notify_audience, render

    tx = taxonomy()
    is_high = severity == "high" or any(f in tx["high"] for f in flags)
    if is_high:
        await create_ticket(s, emp, "alert", text or ", ".join(flags), severity="high")
        if offer_employee:
            emp.state = {**emp.state, "last_problem": text}
            await s.commit()
            await channels.send(
                s,
                emp,
                OutMessage(
                    text=render(
                        "Похоже, ситуация непростая 💙 {Ты не один|Вы не одни}: я могу конфиденциально передать это коллегам из HR — они помогут.",
                        emp,
                    ),
                    buttons=[
                        [Button("📨 Да, передай HR", payload="a:relay")],
                        [Button("Не сейчас", payload="a:noop")],
                    ],
                ),
            )
    manager_flags = manager_topics([flags], tx)
    if manager_flags:
        await notify_audience(
            s,
            emp,
            "manager",
            f"Сигнал по адаптации: {', '.join(manager_flags)}. Рекомендуем провести личную встречу 1:1 с {emp.full_name} в ближайшие дни.",
            None,
        )
