"""Демо-доступ «попробуйте сами»: выключен по умолчанию, подпись планового времени — по часовому поясу площадки."""

from datetime import date, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from app.bot import demo_try
from app.config import settings

UTC = ZoneInfo("UTC")


def emp(**kw):
    return SimpleNamespace(**({"site_code": "GTP", "start_date": date(2026, 10, 27)} | kw))


def test_try_link_is_off_by_default(monkeypatch):
    monkeypatch.setattr(settings(), "demo_try_payload", "")
    assert not demo_try.is_try("")
    assert not demo_try.is_try("try")


def test_try_link_matches_only_configured_payload(monkeypatch):
    monkeypatch.setattr(settings(), "demo_try_payload", "try")
    assert demo_try.is_try("try")
    assert not demo_try.is_try("staff_try")


def test_plan_label_before_start():
    # 07:00 UTC — 10:00 по Москве: подпись в часах площадки, а не сервера
    assert (
        demo_try.plan_label(datetime(2026, 10, 26, 7, 0, tzinfo=UTC), emp())
        == "за день до выхода, пн 26.10 10:00"
    )


def test_plan_label_first_and_later_days():
    assert (
        demo_try.plan_label(datetime(2026, 10, 27, 7, 0, tzinfo=UTC), emp())
        == "1-й день работы, вт 27.10 10:00"
    )
    assert (
        demo_try.plan_label(datetime(2026, 10, 29, 12, 0, tzinfo=UTC), emp())
        == "3-й день работы, чт 29.10 15:00"
    )


def test_plan_label_days_before_are_plural():
    assert demo_try.plan_label(datetime(2026, 10, 24, 7, 0, tzinfo=UTC), emp()).startswith(
        "за 3 дня до выхода"
    )
    assert demo_try.plan_label(datetime(2026, 10, 22, 7, 0, tzinfo=UTC), emp()).startswith(
        "за 5 дней до выхода"
    )
