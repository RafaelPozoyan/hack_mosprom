"""Формат запросов и разбор событий MAX Bot API — по примерам из https://dev.max.ru/docs-api."""

import asyncio

from app.bot.updates import _button_text, _load_marker, _marker_file
from app.channels.base import Button, OutMessage
from app.channels.max import MaxClient, _keyboard
from app.config import settings


def test_keyboard_matches_docs():
    rows = _keyboard([[Button("Да", payload="b:x:0:0:0"), Button("Сайт", url="https://example.com")]])
    assert rows == [
        [
            {"type": "callback", "text": "Да", "payload": "b:x:0:0:0"},
            {"type": "link", "text": "Сайт", "url": "https://example.com"},
        ]
    ]


def test_message_body_has_inline_keyboard_attachment():
    client = MaxClient("token", "https://platform-api2.max.ru")
    body = asyncio.run(client._body(OutMessage(text="Привет", buttons=[[Button("Ок", payload="a:menu")]])))
    assert body["text"] == "Привет" and body["format"] == "markdown"
    assert body["attachments"] == [
        {
            "type": "inline_keyboard",
            "payload": {"buttons": [[{"type": "callback", "text": "Ок", "payload": "a:menu"}]]},
        }
    ]


def test_auth_header_not_query():
    # «Передача токена через query-параметры больше не поддерживается — используйте заголовок Authorization»
    client = MaxClient("secret-token", "https://platform-api2.max.ru")
    assert client.http.headers["Authorization"] == "secret-token"


def test_callback_button_text_is_recovered_from_message():
    message = {
        "body": {
            "mid": "m1",
            "attachments": [
                {
                    "type": "inline_keyboard",
                    "payload": {
                        "buttons": [[{"type": "callback", "text": "На ВЫ", "payload": "b:welcome:1:0:1"}]]
                    },
                }
            ],
        }
    }
    assert _button_text(message, "b:welcome:1:0:1") == "На ВЫ"
    assert _button_text(message, "unknown") == ""


def test_poll_marker_survives_restart(tmp_path, monkeypatch):
    monkeypatch.setattr(settings(), "data_dir", tmp_path)
    assert _load_marker() is None  # первый запуск: позиции ещё нет
    _marker_file().write_text("1792845123")
    assert _load_marker() == 1792845123
    _marker_file().write_text("мусор")
    assert _load_marker() is None  # испорченный файл не роняет поллинг


def test_poll_marker_advances_only_after_entire_batch_is_saved(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from app.bot import inbox, updates

    monkeypatch.setattr(settings(), "data_dir", tmp_path)
    _marker_file().write_text("10")
    first, second = {"event": 1}, {"event": 2}
    saved, requested = [], []

    async def enqueue(event):
        assert _load_marker() == 10
        saved.append(event)

    async def poll(marker):
        requested.append(marker)
        if len(requested) == 1:
            return [first, second], 20
        assert saved == [first, second]
        assert _load_marker() == 20
        raise asyncio.CancelledError

    monkeypatch.setattr(inbox, "enqueue", enqueue)
    monkeypatch.setattr(updates, "max_client", lambda: SimpleNamespace(poll=poll))
    direct = AsyncMock()
    monkeypatch.setattr(updates, "process_max_update", direct)

    async def run():
        try:
            await updates.poll_loop()
        except asyncio.CancelledError:
            pass

    asyncio.run(run())
    assert requested == [10, 20]
    direct.assert_not_awaited()


def test_poll_retries_same_batch_after_queue_failure(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from app.bot import inbox, updates

    monkeypatch.setattr(settings(), "data_dir", tmp_path)
    _marker_file().write_text("10")
    requested, enqueued = [], []
    failed = False

    async def enqueue(event):
        nonlocal failed
        enqueued.append(event)
        if event == 2 and not failed:
            failed = True
            raise OSError("queue unavailable")

    async def poll(marker):
        requested.append(marker)
        if len(requested) == 1:
            assert _load_marker() == 10
            return [1, 2], 20
        raise asyncio.CancelledError

    monkeypatch.setattr(inbox, "enqueue", enqueue)
    monkeypatch.setattr(updates, "max_client", lambda: SimpleNamespace(poll=poll))
    monkeypatch.setattr(updates.asyncio, "sleep", AsyncMock())

    async def run():
        try:
            await updates.poll_loop()
        except asyncio.CancelledError:
            pass

    asyncio.run(run())
    assert requested == [10, 20]
    assert enqueued == [1, 2, 1, 2]
    assert _load_marker() == 20


def test_poll_null_marker_preserves_known_position(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from app.bot import inbox, updates

    monkeypatch.setattr(settings(), "data_dir", tmp_path)
    _marker_file().write_text("10")
    requested = []

    async def poll(marker):
        requested.append(marker)
        if len(requested) == 1:
            return [], None
        raise asyncio.CancelledError

    monkeypatch.setattr(inbox, "enqueue", AsyncMock())
    monkeypatch.setattr(updates, "max_client", lambda: SimpleNamespace(poll=poll))

    async def run():
        try:
            await updates.poll_loop()
        except asyncio.CancelledError:
            pass

    asyncio.run(run())
    assert requested == [10, 10]
    assert _load_marker() == 10


def test_first_poll_keeps_batch_until_queue_recovers(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from app.bot import inbox, updates

    monkeypatch.setattr(settings(), "data_dir", tmp_path)
    requested, enqueued = [], []

    async def enqueue(event):
        enqueued.append(event)
        if len(enqueued) == 2:
            raise OSError("queue unavailable")

    async def poll(marker):
        requested.append(marker)
        if len(requested) == 1:
            return [1, 2], 20
        assert enqueued == [1, 2, 1, 2]
        raise asyncio.CancelledError

    monkeypatch.setattr(inbox, "enqueue", enqueue)
    monkeypatch.setattr(updates, "max_client", lambda: SimpleNamespace(poll=poll))
    monkeypatch.setattr(updates.asyncio, "sleep", AsyncMock())

    async def run():
        try:
            await updates.poll_loop()
        except asyncio.CancelledError:
            pass

    asyncio.run(run())
    assert requested == [None, 20]
    assert _load_marker() == 20


def test_poll_keeps_batch_after_checkpoint_write_failure(tmp_path, monkeypatch):
    from pathlib import Path
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from app.bot import inbox, updates

    monkeypatch.setattr(settings(), "data_dir", tmp_path)
    _marker_file().write_text("10")
    replace = Path.replace
    attempts, requested = [], []

    def fail_once(path, target):
        attempts.append(path)
        if len(attempts) == 1:
            raise OSError("checkpoint unavailable")
        assert _load_marker() == 10
        return replace(path, target)

    async def poll(marker):
        requested.append(marker)
        if len(requested) == 1:
            return [1], 20
        raise asyncio.CancelledError

    enqueue = AsyncMock()
    monkeypatch.setattr(Path, "replace", fail_once)
    monkeypatch.setattr(inbox, "enqueue", enqueue)
    monkeypatch.setattr(updates, "max_client", lambda: SimpleNamespace(poll=poll))
    monkeypatch.setattr(updates.asyncio, "sleep", AsyncMock())

    async def run():
        try:
            await updates.poll_loop()
        except asyncio.CancelledError:
            pass

    asyncio.run(run())
    assert requested == [10, 20]
    assert enqueue.await_count == 2
    assert _load_marker() == 20
