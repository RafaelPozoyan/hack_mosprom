"""Тесты детерминированной логики: то, на чём держится доверие к боту, не должно зависеть от LLM."""

from datetime import date
from types import SimpleNamespace

from app.bot.scenario import render, step_date
from app.rag.answer import Citation, GroundedAnswer, verify
from app.rag.ingest import chunk_markdown, heading_level, strip_running_headers
from app.rag.search import Hit, rrf


def emp(**kw):
    base = {"first_name": "Анна", "full_name": "Иванова Анна", "position": "Инженер", "site_code": "GTP", "address_form": "ty",
                "start_date": date(2026, 10, 26), "probation_end": date(2027, 1, 26)}
    return SimpleNamespace(**(base | kw))


HIT = Hit(1, 1, "ПВТР", "ПВТР › 6.6. Рабочее время",
          "С понедельника по четверг: начало работы — 9:00, окончание работы - 18:00. Перерыв: 13:00 — 13:48.", 0.9)


# --- контроль галлюцинаций ---------------------------------------------------------------------------


def test_verify_accepts_exact_quote_and_supported_numbers():
    ans = GroundedAnswer(answerable=True, answer="Рабочий день с 9:00 до 18:00.",
                         citations=[Citation(source=1, quote="начало работы — 9:00, окончание работы - 18:00")])
    assert verify(ans, [HIT], "Во сколько начинается рабочий день?") == ("ok", [HIT])


def test_verify_ignores_punctuation_differences_in_quote():
    ans = GroundedAnswer(answerable=True, answer="С 9 до 18.", citations=[Citation(source=1, quote="начало работы 9:00 окончание работы 18:00")])
    assert verify(ans, [HIT], "")[0] == "ok"


def test_verify_rejects_invented_quote():
    ans = GroundedAnswer(answerable=True, answer="С 9:00.", citations=[Citation(source=1, quote="рабочий день начинается в 8:30")])
    assert verify(ans, [HIT], "")[0] == "bad_citation"


def test_verify_rejects_number_not_in_source():
    # цитата настоящая, но в ответе придуманное число — классическая галлюцинация в кадровом вопросе
    ans = GroundedAnswer(answerable=True, answer="Рабочий день с 9:00 до 18:00, по пятницам до 16:00.",
                         citations=[Citation(source=1, quote="начало работы — 9:00")])
    assert verify(ans, [HIT], "")[0] == "unsupported_number"


def test_verify_allows_list_markers_and_numbers_from_question():
    ans = GroundedAnswer(answerable=True, answer="1. Начало в 9:00\n2. Через 3 месяца — то же самое",
                         citations=[Citation(source=1, quote="начало работы — 9:00")])
    assert verify(ans, [HIT], "А через 3 месяца график такой же?")[0] == "ok"


def test_verify_rejects_out_of_range_source():
    ans = GroundedAnswer(answerable=True, answer="С 9:00.", citations=[Citation(source=7, quote="9:00")])
    assert verify(ans, [HIT], "")[0] == "bad_citation"


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
