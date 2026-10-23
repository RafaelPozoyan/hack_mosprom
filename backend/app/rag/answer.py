"""Ответ на вопрос сотрудника строго по базе знаний.

Контроль галлюцинаций — несколько независимых барьеров, LLM не последнее слово:
1. порог уверенности реранкера: при слабых совпадениях LLM даже не вызывается;
2. LLM обязана сказать answerable=false, если ответа в источниках нет;
3. каждая цитата должна дословно (с точностью до пунктуации) найтись в указанном источнике;
4. каждое число в ответе (время, сроки, суммы, пункты) должно встречаться в цитируемых источниках или в вопросе.
Не прошёл любой барьер — бот не отвечает сам, а предлагает передать вопрос HR.
"""

import logging
import re
from dataclasses import dataclass, field

from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app import llm
from app.config import settings
from app.rag.search import Hit, search

log = logging.getLogger(__name__)


class Citation(BaseModel):
    source: int
    # длинные «цитаты» абзацами раздувают генерацию на CPU в разы; для проверки хватает одного предложения
    quote: str = Field(max_length=300)


class GroundedAnswer(BaseModel):
    answerable: bool
    answer: str = Field(max_length=1200)
    citations: list[Citation] = Field(max_length=3)


@dataclass
class Result:
    answered: bool
    text: str
    reason: str  # ok | no_hits | low_score | not_in_context | bad_citation | unsupported_number | llm_error
    best_score: float | None
    hits: list[Hit] = field(default_factory=list)
    sources: list[dict] = field(default_factory=list)


SYSTEM = """Ты — Корсик, дружелюбный ИИ-гид по адаптации новых сотрудников Группы ПОЛИПЛАСТИК. Обращайся к сотруднику на «{form}».

Правила точности (обязательны):
1. Отвечай ТОЛЬКО по фрагментам из раздела ИСТОЧНИКИ. Не используй общие знания о законах, ТК РФ и других компаниях, если этого нет в источниках.
2. Если прямого ответа в источниках нет — answerable=false и пустой answer. Честно передать вопрос HR лучше, чем ошибиться.
3. Не придумывай цифры, сроки, суммы, ссылки, имена и контакты.
4. В citations перечисли до трёх фрагментов, на которых основан ответ: номер источника и дословную цитату — одно предложение или его часть, скопированные без изменений.

Стиль: тепло и по-деловому, коротко (до 5–6 предложений или короткий список), без канцелярита, не больше одного эмодзи. Не упоминай в ответе «источники» и их номера."""


def _norm(s: str) -> str:
    return re.sub(r"[^0-9a-zа-я]", "", s.lower().replace("ё", "е"))


LIST_MARKER = re.compile(r"^\s*\d+[.)]\s", re.MULTILINE)


def _ints(s: str) -> set[int]:
    return {int(x) for x in re.findall(r"\d+", s)}


def verify(ans: GroundedAnswer, hits: list[Hit], question: str) -> tuple[str, list[Hit]]:
    cited: list[Hit] = []
    for c in ans.citations:
        if not 1 <= c.source <= len(hits):
            return "bad_citation", []
        h = hits[c.source - 1]
        q = _norm(c.quote)
        if not q or q not in _norm(f"{h.heading} {h.text}"):
            return "bad_citation", []
        if h not in cited:
            cited.append(h)
    if not cited:
        return "bad_citation", []
    allowed = _ints(question) | set().union(*(_ints(f"{h.heading} {h.text}") for h in cited))
    if not _ints(LIST_MARKER.sub("", ans.answer)) <= allowed:
        return "unsupported_number", []
    return "ok", cited


def context_block(hits: list[Hit]) -> str:
    return "\n\n".join(f"[{i}] {h.heading}\n{h.text}" for i, h in enumerate(hits, 1))


async def answer(s: AsyncSession, question: str, site: str = "", form: str = "ты") -> Result:
    hits = await search(s, question, site)
    if not hits:
        return Result(False, "", "no_hits", None)
    best = hits[0].score
    if settings().rerank_url and best < settings().rerank_min_score:
        return Result(False, "", "low_score", best, hits)
    try:
        ans = await llm.structured(
            [
                {"role": "system", "content": SYSTEM.format(form=form)},
                {"role": "user", "content": f"ВОПРОС: {question}\n\nИСТОЧНИКИ:\n{context_block(hits)}"},
            ],
            GroundedAnswer,
            max_tokens=600,
        )
    except Exception:
        log.exception("LLM answer failed")
        return Result(False, "", "llm_error", best, hits)
    if not ans.answerable or not ans.answer.strip():
        return Result(False, "", "not_in_context", best, hits)
    reason, cited = verify(ans, hits, question)
    if reason != "ok":
        log.info("ответ отклонён проверкой (%s): %s", reason, ans.model_dump_json()[:500])
        return Result(False, "", reason, best, hits)
    quotes = {c.source: c.quote for c in ans.citations}
    sources = [
        {"title": h.title, "heading": h.heading, "quote": quotes.get(hits.index(h) + 1, ""), "chunk_id": h.chunk_id}
        for h in cited
    ]
    return Result(True, ans.answer.strip(), "ok", best, hits, sources)
