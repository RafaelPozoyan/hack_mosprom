"""Клиент MAX Bot API (https://dev.max.ru/docs-api).

Лимиты из документации: не более 2 сообщений в секунду в один чат и 30 rps на platform-api2.max.ru.
Медиа загружаются один раз, токен переиспользуется («Токен можно сохранить и переиспользовать»).
"""

import asyncio
import hashlib
import logging
import time
from collections import deque
from pathlib import Path

import httpx

from app.channels.base import Button, OutMessage
from app.config import settings

log = logging.getLogger(__name__)

PER_CHAT_INTERVAL_S = 0.5  # 2 сообщения/с в один чат
GLOBAL_RPS = 30


class MaxApiError(Exception):
    def __init__(self, status: int, body: str):
        super().__init__(f"MAX API {status}: {body}")
        self.status = status
        self.body = body


class RateLimiter:
    """Скользящее окно в 1 секунду: не больше `rps` вызовов."""

    def __init__(self, rps: int):
        self.rps = rps
        self.calls: deque[float] = deque()
        self.lock = asyncio.Lock()

    async def wait(self) -> None:
        async with self.lock:
            now = time.monotonic()
            while self.calls and now - self.calls[0] >= 1.0:
                self.calls.popleft()
            if len(self.calls) >= self.rps:
                await asyncio.sleep(1.0 - (now - self.calls[0]))
                self.calls.popleft()
            self.calls.append(time.monotonic())


class MaxClient:
    def __init__(self, token: str, base_url: str):
        self.verify: str | bool = settings().max_ca_bundle or True
        self.token = token
        self.http = httpx.AsyncClient(
            base_url=base_url, headers={"Authorization": token}, timeout=60, verify=self.verify
        )
        self._chat_last: dict[str, float] = {}
        self._chat_locks: dict[str, asyncio.Lock] = {}
        self._limiter = RateLimiter(GLOBAL_RPS)
        self._media_tokens: dict[str, str] = {}

    async def _call(self, method: str, path: str, **kw) -> dict:
        await self._limiter.wait()
        r = await self.http.request(method, path, **kw)
        if r.status_code >= 400:
            raise MaxApiError(r.status_code, r.text)
        body = r.json() if r.content else {}
        # подписка, правка сообщения и ответ на нажатие сообщают о неудаче в теле ответа с кодом 200
        if isinstance(body, dict) and body.get("success") is False:
            raise MaxApiError(r.status_code, body.get("message", r.text))
        return body

    async def _paced(self, key: str):
        lock = self._chat_locks.setdefault(key, asyncio.Lock())
        await lock.acquire()
        wait = self._chat_last.get(key, 0) + PER_CHAT_INTERVAL_S - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        return lock

    # --- медиа -------------------------------------------------------------------------------------

    async def upload(self, path: Path, kind: str) -> str:
        """kind: image | file. Возвращает токен вложения (кэшируется по содержимому файла)."""
        data = path.read_bytes()
        key = f"{kind}:{hashlib.sha256(data).hexdigest()}"
        if key in self._media_tokens:
            return self._media_tokens[key]
        slot = await self._call("POST", "/uploads", params={"type": kind})
        async with httpx.AsyncClient(timeout=120, verify=self.verify) as up:
            r = await up.post(
                slot["url"], headers={"Authorization": self.token}, files={"data": (path.name, data)}
            )
        if r.status_code >= 400:
            raise MaxApiError(r.status_code, r.text)
        body = r.json()
        if kind == "image":
            token = next(iter(body["photos"].values()))["token"]
        else:
            token = body.get("token") or slot["token"]
        self._media_tokens[key] = token
        return token

    async def _body(self, msg: OutMessage) -> dict:
        attachments: list[dict] = []
        media = settings().media_dir
        if msg.image:
            attachments.append(
                {"type": "image", "payload": {"token": await self.upload(media / msg.image, "image")}}
            )
        if msg.file:
            attachments.append(
                {"type": "file", "payload": {"token": await self.upload(media / msg.file, "file")}}
            )
        if msg.buttons:
            attachments.append({"type": "inline_keyboard", "payload": {"buttons": _keyboard(msg.buttons)}})
        return {"text": msg.text, "attachments": attachments, "format": "markdown"}

    # --- сообщения ---------------------------------------------------------------------------------

    async def send(self, msg: OutMessage, *, user_id: int | None = None, chat_id: int | None = None) -> str:
        params = {"user_id": user_id} if user_id else {"chat_id": chat_id}
        body = await self._body(msg)
        lock = await self._paced(str(params))
        try:
            for attempt in range(5):
                try:
                    res = await self._call("POST", "/messages", params=params, json=body)
                    return res["message"]["body"]["mid"]
                except MaxApiError as e:
                    # вложение ещё обрабатывается на стороне MAX — повторяем с растущей паузой (рекомендация доки)
                    if "attachment.not.ready" not in e.body or attempt == 4:
                        raise
                    await asyncio.sleep(2**attempt)
        finally:
            self._chat_last[str(params)] = time.monotonic()
            lock.release()
        raise RuntimeError("unreachable")

    async def edit(self, mid: str, msg: OutMessage) -> None:
        await self._call("PUT", "/messages", params={"message_id": mid}, json=await self._body(msg))

    async def answer_callback(self, callback_id: str, notification: str | None = None) -> None:
        # MAX принимает ответ только с message или notification (иначе 400 proto.payload); без них подтверждать
        # нечем — клиент сам снимает ожидание с кнопки (проверено в MAX 24.10)
        if not notification:
            return
        await self._call(
            "POST", "/answers", params={"callback_id": callback_id}, json={"notification": notification}
        )

    # --- события -----------------------------------------------------------------------------------

    async def subscribe(self, url: str, secret: str) -> None:
        await self._call(
            "POST",
            "/subscriptions",
            json={
                "url": url,
                "update_types": ["bot_started", "message_created", "message_callback"],
                **({"secret": secret} if secret else {}),
            },
        )

    async def poll(self, marker: int | None) -> tuple[list[dict], int | None]:
        params = {"timeout": 30, "types": "bot_started,message_created,message_callback"}
        if marker is not None:
            params["marker"] = marker
        res = await self._call("GET", "/updates", params=params)
        return res.get("updates", []), res.get("marker")


def _keyboard(rows: list[list[Button]]) -> list[list[dict]]:
    out = []
    for row in rows:
        out.append(
            [
                {"type": "link", "text": b.text, "url": b.url}
                if b.url
                else {"type": "callback", "text": b.text, "payload": b.payload or b.text}
                for b in row
            ]
        )
    return out


_client: MaxClient | None = None


def max_client() -> MaxClient | None:
    global _client
    if _client is None and settings().max_token:
        _client = MaxClient(settings().max_token, settings().max_api_url)
    return _client
