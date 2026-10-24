"""Ближайшее время дайджеста: календарь проверяется независимо от БД и LLM."""

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app import scheduler


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "now,tz,weekday,send_time,expected",
    [
        ("2026-10-26T08:00:00+03:00", "Europe/Moscow", 0, "09:00", "2026-10-26T09:00:00+03:00"),
        ("2026-10-26T09:00:00+03:00", "Europe/Moscow", 0, "09:00", "2026-11-02T09:00:00+03:00"),
        ("2026-10-26T09:00:01+03:00", "Europe/Moscow", 0, "09:00", "2026-11-02T09:00:00+03:00"),
        ("2026-10-25T23:30:00+00:00", "Europe/Moscow", 0, "09:00", "2026-10-26T09:00:00+03:00"),
        ("2026-10-27T08:00:00+03:00", "Europe/Moscow", 0, "09:00", "2026-11-02T09:00:00+03:00"),
        ("2026-10-30T12:29:00+00:00", "Asia/Yekaterinburg", 4, "17:30", "2026-10-30T17:30:00+05:00"),
    ],
)
async def test_next_digest_uses_local_time(monkeypatch, now, tz, weekday, send_time, expected):
    monkeypatch.setattr(
        scheduler,
        "settings",
        lambda: SimpleNamespace(
            default_tz=tz,
            digest_weekday=weekday,
            digest_time=send_time,
        ),
    )
    s = SimpleNamespace(
        scalar=AsyncMock(return_value=None), execute=AsyncMock(), add=Mock(), commit=AsyncMock()
    )
    await scheduler.ensure_digest_job(s, after=datetime.fromisoformat(now))
    s.add.assert_called_once()
    job = s.add.call_args.args[0]
    assert job.kind == "digest" and job.run_at.isoformat() == expected
    s.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_startup_uses_clock_and_keeps_existing_pending_digest(monkeypatch):
    now = datetime.fromisoformat("2026-10-26T08:00:00+03:00")
    monkeypatch.setattr(scheduler, "utcnow", lambda: now)
    monkeypatch.setattr(
        scheduler,
        "settings",
        lambda: SimpleNamespace(
            default_tz="Europe/Moscow",
            digest_weekday=0,
            digest_time="09:00",
        ),
    )
    s = SimpleNamespace(scalar=AsyncMock(return_value=1), execute=AsyncMock(), add=Mock(), commit=AsyncMock())
    await scheduler.ensure_digest_job(s)
    s.add.assert_not_called()
    s.scalar.return_value = None
    await scheduler.ensure_digest_job(s)
    assert s.add.call_args.args[0].run_at.isoformat() == "2026-10-26T09:00:00+03:00"
