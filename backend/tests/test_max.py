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
