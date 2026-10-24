"""OpenAI-совместимый клиент: один код для локальной GigaChat 3.1 Lightning (llama.cpp/vLLM), YandexGPT и GigaChat API."""

import json
import logging
import re

from openai import AsyncOpenAI
from pydantic import BaseModel, ValidationError

from app.config import settings

log = logging.getLogger(__name__)

_client: AsyncOpenAI | None = None


def client() -> AsyncOpenAI:
    global _client
    if _client is None:
        cfg = settings()
        # без автоповторов: повтор долгого запроса к LLM на CPU только утраивает ожидание пользователя
        _client = AsyncOpenAI(
            base_url=cfg.llm_base_url, api_key=cfg.llm_api_key, timeout=cfg.llm_timeout_s, max_retries=0
        )
    return _client


async def complete(messages: list[dict], temperature: float = 0.2) -> str:
    r = await client().chat.completions.create(
        model=settings().llm_model, messages=messages, temperature=temperature
    )
    return r.choices[0].message.content or ""


async def structured[T: BaseModel](
    messages: list[dict], schema: type[T], temperature: float = 0.0, max_tokens: int = 1024
) -> T:
    """Ответ строго по JSON-схеме. Если провайдер не поддерживает json_schema — схема уходит в промпт."""
    cfg = settings()
    kwargs: dict = {}
    if cfg.llm_json_schema:
        kwargs["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": schema.__name__, "schema": schema.model_json_schema(), "strict": True},
        }
    else:
        messages = [
            *messages[:-1],
            {
                "role": messages[-1]["role"],
                "content": messages[-1]["content"]
                + "\n\nОтветь только JSON по схеме:\n"
                + json.dumps(schema.model_json_schema(), ensure_ascii=False),
            },
        ]
    last_error: Exception | None = None
    for _ in range(2):
        r = await client().chat.completions.create(
            model=cfg.llm_model, messages=messages, temperature=temperature, max_tokens=max_tokens, **kwargs
        )
        raw = r.choices[0].message.content or ""
        try:
            return schema.model_validate_json(_extract_json(raw))
        except (ValidationError, ValueError) as e:
            last_error = e
            log.warning("LLM вернула невалидный JSON: %s", raw[:300])
    raise ValueError(f"LLM structured output failed: {last_error}")


def _extract_json(raw: str) -> str:
    raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", raw, flags=re.DOTALL)
    if fence:
        raw = fence.group(1)
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("no JSON object in LLM output")
    return raw[start : end + 1]
