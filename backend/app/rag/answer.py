"""Ответ на вопрос сотрудника строго по базе знаний.

Контроль галлюцинаций — несколько независимых барьеров, LLM не последнее слово:
1. порог уверенности реранкера: при слабых совпадениях LLM даже не вызывается;
2. LLM обязана сказать answerable=false, если ответа в источниках нет;
3. цитата должна дословно (с точностью до пунктуации и окончаний слов) найтись в одном из найденных фрагментов;
   пропуск слов модель обязана отметить многоточием. Цитата, которой в фрагментах нет, отбрасывается и сотруднику
   не показывается; без хотя бы одной подтверждённой цитаты ответа нет;
4. каждое число в ответе (время, сроки, суммы, пункты) должно встречаться в подтверждённых фрагментах или в вопросе —
   время и даты целиком, а не по отдельным цифрам; e-mail и ссылки — дословно. Числа словами не сверяются.
Не прошёл любой барьер — бот не отвечает сам, а предлагает передать вопрос HR.
"""

import logging
import os
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
    # Порядок полей = порядок генерации: сначала опора из источников, потом решение «есть ли прямой ответ», потом ответ.
    # eval: без минимума в одну цитату модель оставляла список пустым в 7 из 18 отклонённых ответов, а с answerable
    # первым полем решала «ответа нет» до чтения источников — 5 из 17 верных ответов с верными цитатами.
    citations: list[Citation] = Field(min_length=1, max_length=3)
    answerable: bool
    answer: str = Field(max_length=1200)


@dataclass
class Result:
    answered: bool
    text: str
    # ok | no_hits | low_score | not_in_context | bad_citation | unsupported_number | unsupported_contact | llm_error | search_error
    reason: str
    best_score: float | None
    hits: list[Hit] = field(default_factory=list)
    sources: list[dict] = field(default_factory=list)


SYSTEM = """Ты — Корсик, дружелюбный ИИ-гид по адаптации новых сотрудников Группы ПОЛИПЛАСТИК. Обращайся к сотруднику на «{form}».

Правила точности (обязательны):
1. Отвечай ТОЛЬКО по фрагментам из раздела ИСТОЧНИКИ. Не используй общие знания о законах, ТК РФ и других компаниях, если этого нет в источниках.
2. Если в выписанных фрагментах нет прямого ответа на вопрос — answerable=false и пустой answer. Честно передать вопрос HR лучше, чем ошибиться.
3. Не придумывай цифры, сроки, суммы, ссылки, имена и контакты.
4. Сначала заполни citations — от одного до трёх фрагментов, ближе всего относящихся к вопросу. source — номер источника из квадратных скобок ([1], [2] …), а не номер пункта документа. quote — одно предложение или его часть, скопированные символ в символ, даже если в тексте опечатка; пропущенные слова замени многоточием. Затем реши answerable и напиши answer только по этим фрагментам.

Стиль: тепло и по-деловому, коротко (до 5–6 предложений или короткий список), без канцелярита, не больше одного эмодзи. Не упоминай в ответе «источники» и их номера."""


def _norm(s: str) -> str:
    return re.sub(r"[^0-9a-zа-я]", "", s.lower().replace("ё", "е"))


LIST_MARKER = re.compile(r"^\s*\d+[.)]\s", re.MULTILINE)
ELLIPSIS = re.compile(r"\.{3,}|…")


# «одно предложение или его часть» (промпт): короче трёх слов цитата ничего не подтверждает — «и», «работы» есть везде
MIN_QUOTE_WORDS = 3
WORD = re.compile(r"[0-9a-zа-яё]+")
NUMBER = re.compile(r"\d+(?:[:.,]\d+)*")
CONTACT = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+|https?://[^\s)»\"]+", re.IGNORECASE)


def _numbers(s: str) -> set[str]:
    """Числа в каноническом виде («09.00» и «9:00» → «9:0»): время и даты сверяются целиком, а не по цифрам."""
    return {":".join(str(int(x)) for x in re.split(r"[:.,]", n)) for n in NUMBER.findall(s)}


def _words(s: str) -> list[str]:
    return WORD.findall(s.lower().replace("ё", "е"))


def _same_word(a: str, b: str) -> bool:
    """Слова равны или отличаются только окончанием (до трёх букв: «выдача» — «выдачу»): модель склоняет слова цитаты.
    Числа — только точно: «28» не становится «29»."""
    stem = len(os.path.commonprefix([a, b]))
    return a == b or (a.isalpha() and b.isalpha() and stem >= 4 and max(len(a), len(b)) - stem <= 3)


def _has_run(text: list[str], part: list[str]) -> bool:
    """Слова куска цитаты идут в тексте подряд, одно к одному: пропустить слово («не») нельзя."""
    n = len(part)
    return any(all(map(_same_word, part, text[i : i + n])) for i in range(len(text) - n + 1))


def _quoted_in(quote: str, h: Hit) -> bool:
    text = f"{h.heading} {h.text}"
    parts = [p for p in ELLIPSIS.split(quote) if _words(p)]
    # точное совпадение без пунктуации ловит перенос «законода-тельством» в распознанном скане, пословное — окончания
    return bool(parts) and all(_norm(p) in _norm(text) or _has_run(_words(text), _words(p)) for p in parts)


def verify(ans: GroundedAnswer, hits: list[Hit], question: str) -> tuple[str, list[tuple[Hit, str]]]:
    """Возвращает причину и подтверждённые пары (фрагмент, цитата).
    Неподтверждённая цитата отбрасывается, а не губит ответ: в eval модель добавляла к верной цитате лишнюю,
    склеенную из разных мест, — 3 верных ответа из 22 уходили HR. Числа и контакты сверяются только
    с подтверждёнными фрагментами, поэтому опереться на отброшенную цитату ответ не может."""
    cited: list[tuple[Hit, str]] = []
    for c in ans.citations:
        if len(WORD.findall(c.quote.lower())) < MIN_QUOTE_WORDS:
            continue
        # номер источника — только подсказка: модель путает его с номером пункта («6.6.3» → 6), цитату ищем во всех
        claimed = [hits[c.source - 1]] if 1 <= c.source <= len(hits) else []
        h = next((h for h in claimed + hits if _quoted_in(c.quote, h)), None)
        if h is not None and h not in [x for x, _ in cited]:
            cited.append((h, c.quote))
    if not cited:
        return "bad_citation", []
    sources = " ".join(f"{h.heading} {h.text}" for h, _ in cited)
    known = _numbers(question) | _numbers(sources)
    parts = {p for n in known for p in n.split(":")}
    # целое число может быть частью времени или даты источника («9» из «9:00»), а время или дата — только целиком
    if any(n not in known if ":" in n else n not in parts for n in _numbers(LIST_MARKER.sub("", ans.answer))):
        return "unsupported_number", []
    if any(c.lower().rstrip(".,;") not in sources.lower() for c in CONTACT.findall(ans.answer)):
        return "unsupported_contact", []
    return "ok", cited


def context_block(hits: list[Hit]) -> str:
    return "\n\n".join(f"[{i}] {h.heading}\n{h.text}" for i, h in enumerate(hits, 1))


# Ответ при той же базе знаний не меняется (температура 0), а поиск с реранкером на CPU обрабатывает вопросы по очереди:
# повтор вопроса отдаём из памяти. Важно для демо, где 5–10 человек одновременно задают одни и те же вопросы.
_answers: dict[tuple[str, str, str], Result] = {}
ANSWER_CACHE_SIZE = 500


def forget_answers() -> None:
    """База знаний изменилась — запомненные ответы могли устареть."""
    _answers.clear()


async def answer(s: AsyncSession, question: str, site: str = "", form: str = "ты") -> Result:
    key = (" ".join(_words(question)), site, form)
    if key in _answers:
        return _answers[key]
    res = await _answer(s, question, site, form)
    # Запоминаем ответ и отказ поиска — при той же базе они повторятся. Отказ проверки ответа модели
    # и сбой сервиса — нет: следующая попытка может пройти, а отказ не должен достаться всем, кто спросит.
    if res.answered or res.reason in ("no_hits", "low_score"):
        if len(_answers) >= ANSWER_CACHE_SIZE:
            _answers.clear()
        _answers[key] = res
    return res


async def _answer(s: AsyncSession, question: str, site: str = "", form: str = "ты") -> Result:
    try:
        hits = await search(s, question, site)
    except Exception:
        # поиск недоступен (сервис эмбеддингов перезапускается, сеть): сотруднику — предложение спросить HR, а не тишина
        log.exception("search failed")
        return Result(False, "", "search_error", None)
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
    sources = [
        {"title": h.title, "heading": h.heading, "quote": quote, "chunk_id": h.chunk_id} for h, quote in cited
    ]
    return Result(True, ans.answer.strip(), "ok", best, hits, sources)
