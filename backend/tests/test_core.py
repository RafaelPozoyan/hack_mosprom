"""Тесты детерминированной логики: то, на чём держится доверие к боту, не должно зависеть от LLM."""

import asyncio
from datetime import date
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.bot.scenario import render, step_date
from app.rag.answer import Citation, GroundedAnswer, verify
from app.rag.ingest import chunk_markdown, heading_level, strip_running_headers
from app.rag.search import Hit, rrf


def emp(**kw):
    base = {
        "first_name": "Анна",
        "full_name": "Иванова Анна",
        "position": "Инженер",
        "site_code": "GTP",
        "address_form": "ty",
        "start_date": date(2026, 10, 26),
        "probation_end": date(2027, 1, 26),
    }
    return SimpleNamespace(**(base | kw))


HIT = Hit(
    1,
    1,
    "ПВТР",
    "ПВТР › 6.6. Рабочее время",
    "С понедельника по четверг: начало работы — 9:00, окончание работы - 18:00. Перерыв: 13:00 — 13:48.",
    0.9,
)


# --- контроль галлюцинаций ---------------------------------------------------------------------------


def test_verify_accepts_exact_quote_and_supported_numbers():
    ans = GroundedAnswer(
        answerable=True,
        answer="Рабочий день с 9:00 до 18:00.",
        citations=[Citation(source=1, quote="начало работы — 9:00, окончание работы - 18:00")],
    )
    assert verify(ans, [HIT], "Во сколько начинается рабочий день?") == (
        "ok",
        [(HIT, "начало работы — 9:00, окончание работы - 18:00")],
    )


def test_verify_ignores_punctuation_differences_in_quote():
    ans = GroundedAnswer(
        answerable=True,
        answer="С 9 до 18.",
        citations=[Citation(source=1, quote="начало работы 9:00 окончание работы 18:00")],
    )
    assert verify(ans, [HIT], "")[0] == "ok"


def test_verify_rejects_invented_quote():
    ans = GroundedAnswer(
        answerable=True,
        answer="С 9:00.",
        citations=[Citation(source=1, quote="рабочий день начинается в 8:30")],
    )
    assert verify(ans, [HIT], "")[0] == "bad_citation"


def test_verify_rejects_number_not_in_source():
    # цитата настоящая, но в ответе придуманное число — классическая галлюцинация в кадровом вопросе
    ans = GroundedAnswer(
        answerable=True,
        answer="Рабочий день с 9:00 до 18:00, по пятницам до 16:00.",
        citations=[Citation(source=1, quote="начало работы — 9:00")],
    )
    assert verify(ans, [HIT], "")[0] == "unsupported_number"


def test_verify_allows_list_markers_and_numbers_from_question():
    ans = GroundedAnswer(
        answerable=True,
        answer="1. Начало в 9:00\n2. Через 3 месяца — то же самое",
        citations=[Citation(source=1, quote="начало работы — 9:00")],
    )
    assert verify(ans, [HIT], "А через 3 месяца график такой же?")[0] == "ok"


def test_verify_finds_quote_when_model_confuses_source_number():
    # модель путает номер источника с номером пункта документа («6.6.3» → источник 6) — цитата при этом настоящая
    other = Hit(
        2, 1, "ПВТР", "ПВТР › 6.7. Время отдыха", "Ежегодный оплачиваемый отпуск — 28 календарных дней.", 0.5
    )
    ans = GroundedAnswer(
        answerable=True, answer="С 9:00.", citations=[Citation(source=6, quote="начало работы — 9:00")]
    )
    assert verify(ans, [other, HIT], "") == ("ok", [(HIT, "начало работы — 9:00")])


def test_verify_accepts_quote_with_ellipsis():
    ans = GroundedAnswer(
        answerable=True,
        answer="С 9:00 до 18:00.",
        citations=[Citation(source=1, quote="С понедельника по четверг… окончание работы - 18:00")],
    )
    assert verify(ans, [HIT], "")[0] == "ok"


def test_verify_rejects_ellipsis_joining_text_absent_from_source():
    # многоточие разрешает пропуск, но каждый кусок цитаты обязан дословно быть в том же документе
    ans = GroundedAnswer(
        answerable=True,
        answer="С 9:00.",
        citations=[Citation(source=1, quote="начало работы — 9:00... по субботам")],
    )
    assert verify(ans, [HIT], "")[0] == "bad_citation"


def test_verify_tolerates_word_endings_changed_by_model():
    # модель склоняет слова цитаты («начала» вместо «начало») — смысл и все слова на месте
    ans = GroundedAnswer(
        answerable=True, answer="С 9:00.", citations=[Citation(source=1, quote="до начала работы — 9:00")]
    )
    hit = Hit(1, 1, "ПВТР", "ПВТР › 6.6", "Время до начало работы — 9:00 по местному времени.", 0.9)
    assert verify(ans, [hit], "")[0] == "ok"


def test_verify_rejects_quote_that_drops_negation():
    hit = Hit(
        1,
        1,
        "ПВТР",
        "ПВТР › 6.6",
        "Работодатель не допускает к работе в ночное время беременных женщин.",
        0.9,
    )
    ans = GroundedAnswer(
        answerable=True,
        answer="Да, можно.",
        citations=[Citation(source=1, quote="Работодатель допускает к работе в ночное время")],
    )
    assert verify(ans, [hit], "")[0] == "bad_citation"


def test_verify_rejects_quote_with_changed_number():
    hit = Hit(
        1,
        1,
        "ПВТР",
        "ПВТР › 6.7",
        "Ежегодный оплачиваемый отпуск продолжительностью 28 календарных дней.",
        0.9,
    )
    ans = GroundedAnswer(
        answerable=True,
        answer="Отпуск 28 дней.",
        citations=[Citation(source=1, quote="отпуск продолжительностью 29 календарных дней")],
    )
    assert verify(ans, [hit], "")[0] == "bad_citation"


def test_verify_drops_unconfirmed_citation_and_checks_numbers_only_against_confirmed():
    other = Hit(
        2, 1, "ПВТР", "ПВТР › 6.7. Время отдыха", "Ежегодный оплачиваемый отпуск — 28 календарных дней.", 0.5
    )
    real = Citation(source=1, quote="начало работы — 9:00, окончание работы - 18:00")
    invented = Citation(source=2, quote="отпуск продлевается до 35 календарных дней")
    # придуманная цитата отбрасывается и не показывается, ответ держится на подтверждённой
    ans = GroundedAnswer(answerable=True, answer="Работаем с 9:00 до 18:00.", citations=[real, invented])
    assert verify(ans, [HIT, other], "") == ("ok", [(HIT, real.quote)])
    # число из фрагмента, на который не нашлось подтверждённой цитаты, ответ не подтверждает
    ans = GroundedAnswer(
        answerable=True, answer="Работаем с 9:00, отпуск 28 дней.", citations=[real, invented]
    )
    assert verify(ans, [HIT, other], "")[0] == "unsupported_number"


def test_verify_rejects_quote_too_short_to_prove_anything():
    # «и» или «работы» есть почти в любом тексте — такая цитата не подтверждает ответ
    ans = GroundedAnswer(
        answerable=True,
        answer="Работа на дому разрешена по пятницам.",
        citations=[Citation(source=1, quote="работы")],
    )
    assert verify(ans, [HIT], "")[0] == "bad_citation"


def test_verify_rejects_time_assembled_from_other_numbers():
    # в источнике есть 13:00, 13:48 и 18:00, но не 13:18 — сверяем время целиком, а не отдельные цифры
    ans = GroundedAnswer(
        answerable=True,
        answer="Перерыв с 13:18.",
        citations=[Citation(source=1, quote="Перерыв: 13:00 — 13:48")],
    )
    assert verify(ans, [HIT], "")[0] == "unsupported_number"


def test_verify_accepts_time_written_differently():
    ans = GroundedAnswer(
        answerable=True,
        answer="Начало в 09.00, а перерыв — в 13 часов.",
        citations=[Citation(source=1, quote="начало работы — 9:00, окончание работы - 18:00")],
    )
    assert verify(ans, [HIT], "")[0] == "ok"


def test_verify_rejects_contact_not_in_source():
    ans = GroundedAnswer(
        answerable=True,
        answer="Пишите на hr@polyplastic.ru, начало в 9:00.",
        citations=[Citation(source=1, quote="начало работы — 9:00")],
    )
    assert verify(ans, [HIT], "")[0] == "unsupported_contact"


def test_verify_accepts_contact_from_source():
    hit = Hit(
        3,
        2,
        "Антикоррупционная политика",
        "Проверка контрагентов",
        "Запрос на проверку направляйте на proverka@polyplastic.ru до заключения договора.",
        0.8,
    )
    ans = GroundedAnswer(
        answerable=True,
        answer="Отправьте запрос на proverka@polyplastic.ru.",
        citations=[Citation(source=1, quote="Запрос на проверку направляйте на proverka@polyplastic.ru")],
    )
    assert verify(ans, [hit], "")[0] == "ok"


def test_answer_without_citations_is_invalid():
    with pytest.raises(ValidationError):
        GroundedAnswer(answerable=True, answer="С 9:00.", citations=[])


# --- поиск -------------------------------------------------------------------------------------------


def test_rrf_rewards_agreement_between_rankers():
    fused = dict(rrf([[1, 2, 3], [3, 1, 4]], k=60))
    assert fused[1] > fused[2] and fused[3] > fused[2] and fused[1] > fused[4]


# --- документы ---------------------------------------------------------------------------------------


def test_running_headers_and_stamps_are_removed():
    page = "| ООО «Группа ПОЛИПЛАСТИК» | Правила | Лист {n} Листов 23 |\n|---|---|---|\n## ООО «Группа ПОЛИПЛАСТИК»\n\nПункт {n}."
    md = "\n\n".join(page.format(n=n) for n in range(1, 5))
    clean = strip_running_headers(md)
    assert "ПОЛИПЛАСТИК" not in clean and "Лист" not in clean
    assert all(f"Пункт {n}." in clean for n in range(1, 5))


def test_numbered_headings_build_hierarchy():
    assert heading_level(2, "6. ПРАВИЛА") == 1
    assert heading_level(2, "6.4. Основные права") == 2
    assert heading_level(2, "6.4.1, Работник имеет право на:") == 3
    md = "## 6. ПРАВИЛА\n## 6.4. Права\n## 6.4.1. Работник имеет право на:\nотдых\n## 6.5. Обязанности\nтруд"
    chunks = chunk_markdown(md, "ПВТР", 1500)
    assert chunks[0] == ("ПВТР › 6. ПРАВИЛА › 6.4. Права › 6.4.1. Работник имеет право на:", "отдых")
    assert chunks[1][0] == "ПВТР › 6. ПРАВИЛА › 6.5. Обязанности"


def test_long_sections_are_split_by_sentences():
    text = " ".join(f"Предложение номер {i}." for i in range(200))
    pieces = chunk_markdown(f"## Раздел\n{text}", "Док", 500)
    assert len(pieces) > 1 and all(len(t) <= 500 for _, t in pieces)


# --- сценарий ----------------------------------------------------------------------------------------


def test_render_address_form_variants():
    text = "Привет, {name}! {Ты готов|Вы готовы}?"
    assert render(text, emp(address_form="ty")) == "Привет, Анна! Ты готов?"
    assert render(text, emp(address_form="vy")) == "Привет, Анна! Вы готовы?"


def test_timeline_dates_follow_working_days():
    e = emp(start_date=date(2026, 10, 23))  # пятница
    assert step_date({"workday": 1}, e) == date(2026, 10, 23)
    assert step_date({"workday": 2}, e) == date(2026, 10, 26)  # понедельник
    assert step_date({"day": -1}, e) == date(2026, 10, 22)
    assert step_date({"weeks": 2}, e) == date(2026, 11, 6)
    assert step_date({"months": 1, "weeks": 2}, e) == date(2026, 12, 7)  # 6.12 — воскресенье → понедельник
    assert step_date({"month_start": 2}, e) == date(2026, 11, 23)


def test_russian_holidays_are_skipped():
    e = emp(start_date=date(2026, 11, 3))  # 4 ноября — День народного единства
    assert step_date({"workday": 2}, e) == date(2026, 11, 5)


# --- раздача панели ----------------------------------------------------------------------------------


def test_spa_never_serves_files_outside_build(tmp_path):
    from app.main import spa_file

    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("app")
    (dist / "assets" / "app.js").write_text("js")
    (tmp_path / ".env").write_text("MAX_TOKEN=secret")
    assert spa_file(dist, "assets/app.js") == (dist / "assets" / "app.js").resolve()
    assert spa_file(dist, "employees/1") == dist / "index.html"
    assert spa_file(dist, "../.env") == dist / "index.html"
    assert spa_file(dist, "") == dist / "index.html"


# --- повторные нажатия ---------------------------------------------------------------------------------


class FakeSession:
    def __init__(self):
        self.info = {}
        self.added = []

    async def commit(self):
        pass

    def add(self, x):
        self.added.append(x)

    async def execute(self, statement):
        pass


def test_path_changing_button_fires_once(monkeypatch):
    # двойной тап по «Нет» под вопросом о доступах: один ответ и один push руководителю, а не два
    from app.analytics import sentiment
    from app.bot import dialog
    from app.models import SurveyAnswer

    reacts = []

    async def fake_send(s, emp, msg, **kw):
        pass

    async def fake_react(s, emp, flags, severity, text, offer_employee=False):
        reacts.append(flags)

    monkeypatch.setattr(dialog, "find_button", lambda payload, emp: {"text": "Нет", "flag": "нет доступов"})
    monkeypatch.setattr(dialog.channels, "send", fake_send)
    monkeypatch.setattr(sentiment, "react", fake_react)
    s, e = FakeSession(), emp(id=1, state={"required_step": {"id": "d1_access"}})
    for _ in range(2):
        asyncio.run(dialog.scenario_button(s, e, "b:d1_access:0:0:1"))
    assert sum(isinstance(x, SurveyAnswer) for x in s.added) == 1
    assert reacts == [["нет доступов"]]


def test_repeated_press_is_answered_instead_of_silence(monkeypatch):
    from app.bot import dialog

    sent = []

    async def fake_send(s, emp, msg, **kw):
        sent.append(msg.text)

    monkeypatch.setattr(
        dialog, "find_button", lambda payload, emp: {"text": "Да, расскажи", "goto": "gym_info"}
    )
    monkeypatch.setattr(dialog.channels, "send", fake_send)
    e = emp(id=1, address_form="vy", state={"pressed": ["d3_gym:0"]})
    asyncio.run(dialog.scenario_button(FakeSession(), e, "b:d3_gym:0:0:0"))
    assert sent == ["Этот вариант уже выбран 👌 Если нужно что-то ещё — напишите своими словами."]


def test_stale_skip_does_not_advance_survey(monkeypatch):
    from app.bot import surveys

    advanced = []

    async def fake_advance(s, emp):
        advanced.append(True)

    monkeypatch.setattr(surveys, "advance", fake_advance)
    e = emp(state={"survey": {"id": "day3", "q": 1, "selected": []}})
    asyncio.run(surveys.comment(FakeSession(), e, None))
    assert advanced == []


# --- приватность руководителя ---------------------------------------------------------------------------


def test_manager_never_sees_topics_from_a_record_about_himself():
    from app.analytics.sentiment import manager_topics

    tx = {
        "manager_actionable": ["недостаток информации", "нет доступов"],
        "never_to_manager": ["конфликт с наставником или руководителем"],
    }
    # в первой записи соседствуют «недостаток информации» и жалоба на руководителя — запись не показывается целиком
    flags = [["недостаток информации", "конфликт с наставником или руководителем"], ["нет доступов"]]
    assert manager_topics(flags, tx) == ["нет доступов"]


@pytest.mark.parametrize(
    ("value", "words"),
    [
        ({"option": "c", "label": "Пока ничего не понял"}, "Пока ничего не понял"),
        ({"options": ["a", "b"], "labels": ["Коллеги", "Наставник"]}, "Коллеги, Наставник"),
        ({"score": 3}, "3 из 10"),
        ({"answer": "no"}, "Нет"),
    ],
)
def test_survey_answer_words(value, words):
    from app.bot import surveys

    assert surveys.answer_words(value) == words


def test_survey_ticket_subject_starts_with_newcomer_words():
    from app.bot import surveys

    subject = surveys.ticket_subject("1) С каким настроением ты приходишь на работу?", {"label": "Уже устал"})
    assert subject == "Уже устал — С каким настроением ты приходишь на работу?"


def test_answer_cache_reuses_same_question_until_knowledge_changes(monkeypatch):
    from unittest.mock import AsyncMock

    from app.rag import answer as rag

    rag.forget_answers()
    ok = rag.Result(True, "28 дней", "ok", 0.9)
    inner = AsyncMock(return_value=ok)
    monkeypatch.setattr(rag, "_answer", inner)
    assert asyncio.run(rag.answer(None, "Сколько дней отпуска?", "GTP", "ты")) is ok
    assert asyncio.run(rag.answer(None, "  сколько дней ОТПУСКА ", "GTP", "ты")) is ok
    assert inner.await_count == 1  # повтор того же вопроса — без поиска и модели
    asyncio.run(rag.answer(None, "Сколько дней отпуска?", "LUZ", "ты"))
    assert inner.await_count == 2  # другая площадка — свой ответ
    rag.forget_answers()
    asyncio.run(rag.answer(None, "Сколько дней отпуска?", "GTP", "ты"))
    assert inner.await_count == 3  # база знаний изменилась — считаем заново


@pytest.mark.parametrize(
    ("reason", "calls"),
    [
        ("llm_error", 2),
        ("search_error", 2),
        ("not_in_context", 2),
        ("bad_citation", 2),
        ("unsupported_number", 2),
        ("unsupported_contact", 2),
        ("low_score", 1),
        ("no_hits", 1),
    ],
)
def test_answer_cache_keeps_only_stable_refusals(monkeypatch, reason, calls):
    from unittest.mock import AsyncMock

    from app.rag import answer as rag

    rag.forget_answers()
    inner = AsyncMock(return_value=rag.Result(False, "", reason, None))
    monkeypatch.setattr(rag, "_answer", inner)
    asyncio.run(rag.answer(None, "Когда зарплата?", "GTP", "ты"))
    asyncio.run(rag.answer(None, "Когда зарплата?", "GTP", "ты"))
    # отказ поиска при той же базе повторится; сбой сервиса и отказ проверки ответа — следующая попытка может пройти
    assert inner.await_count == calls


def test_analysis_skips_deleted_employee(monkeypatch):
    from contextlib import asynccontextmanager
    from unittest.mock import AsyncMock

    from app import db
    from app.analytics import sentiment

    class Empty:
        async def get(self, model, ident):
            return None  # новичка удалили, пока ответ ждал разбора

    @asynccontextmanager
    async def session():
        yield Empty()

    monkeypatch.setattr(db, "session", session)
    analyze = AsyncMock()
    save = AsyncMock()
    monkeypatch.setattr(sentiment, "analyze_text", analyze)
    monkeypatch.setattr(sentiment, "save_and_react", save)
    monkeypatch.setattr(sentiment, "last_bot_text", AsyncMock(return_value=""))

    async def run():
        sentiment.analyze_later(404, "Уже устал", "chat")
        await asyncio.sleep(0.05)

    asyncio.run(run())
    analyze.assert_not_awaited()
    save.assert_not_awaited()
