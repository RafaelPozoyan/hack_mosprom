from functools import lru_cache
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=(ROOT.parent / ".env", ROOT / ".env"), extra="ignore")

    database_url: str = "postgresql+asyncpg://korsik:korsik@localhost:55433/korsik"
    admin_token: str = ""  # пусто — админка без авторизации (только для локального демо)
    public_base_url: str = "http://localhost:8000"

    # MAX Bot API (https://dev.max.ru/docs-api). Без токена работает только демо-канал в веб-админке.
    max_token: str = ""
    max_api_url: str = "https://platform-api2.max.ru"
    max_bot_name: str = ""  # никнейм бота для диплинка https://max.ru/<bot>?start=<payload>
    max_webhook_url: str = ""  # пусто — long polling (только для разработки, см. доку MAX)
    max_webhook_secret: str = ""
    max_hr_chat_id: int | None = None  # групповой чат HR, куда бот публикует эскалации
    max_ca_bundle: str = ""  # путь к сертификату Минцифры, если он не в системном хранилище

    # LLM — любой OpenAI-совместимый endpoint: llama.cpp/vLLM (GigaChat 3.1 Lightning), Yandex AI Studio, GigaChat
    llm_base_url: str = "http://localhost:8081/v1"
    llm_api_key: str = "local"
    llm_model: str = "gigachat-3.1-lightning"
    llm_json_schema: bool = True  # response_format=json_schema; выключить, если провайдер не поддерживает
    llm_timeout_s: float = 180.0

    # Эмбеддинги и реранкер — Text Embeddings Inference (TEI)
    embed_url: str = "http://localhost:8082"
    embed_dim: int = 1024  # BAAI/bge-m3
    rerank_url: str = "http://localhost:8083"  # BAAI/bge-reranker-v2-m3; пусто — без реранкинга
    docling_url: str = "http://localhost:5001"
    ocr_preset: str = "tesseract"  # Tesseract LSTM + tessdata_best/rus — чистые сканы 300 dpi распознаются почти без ошибок
    ocr_lang: str = "rus"

    # Чанкинг и поиск. Размер чанка и число кандидатов подбираются прогоном eval/run_eval.py.
    chunk_max_chars: int = 1500
    # RRF k=60 — константа из статьи Cormack et al., 2009 (Reciprocal Rank Fusion).
    rrf_k: int = 60
    retrieve_candidates: int = 30
    rerank_candidates: int = 20  # eval: гибридный поиск находит нужный фрагмент в топ-20 в 100% случаев
    rerank_top_n: int = 3  # eval: Hit@3 = Hit@5 (97%) — больше источников в промпте не дают качества, только задержку
    # Порог уверенности реранкера, ниже которого бот не отвечает, а предлагает HR.
    # Калибруется скриптом eval/run_eval.py на размеченном наборе вопросов: у вопросов с ответом лучший фрагмент
    # получает не ниже 0.041, у вопросов без ответа — не выше 0.032; порог — середина этого зазора.
    rerank_min_score: float = 0.037

    # Таймлайн
    default_tz: str = "Europe/Moscow"
    default_send_time: str = "10:00"  # время первого сообщения дня 1 по ТЗ; для шагов без явного времени
    probation_months: int = 3  # «в течение первых трех месяцев я буду сопровождать тебя» (ТЗ)
    scheduler_tick_s: float = 15.0
    digest_weekday: int = 0  # понедельник
    digest_time: str = "09:00"

    data_dir: Path = ROOT / "data"
    scenario_path: Path = ROOT / "scenario" / "korsik.yaml"
    media_dir: Path = ROOT / "media"
    kb_dir: Path = ROOT / "kb"

    @field_validator("max_hr_chat_id", mode="before")
    @classmethod
    def _empty_to_none(cls, v):
        return None if v == "" else v


@lru_cache
def settings() -> Settings:
    return Settings()
