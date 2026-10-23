import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.dialects.postgresql import insert

from app import scheduler
from app.api import admin, demo
from app.bot.scenario import scenario
from app.bot.updates import poll_loop, process_max_update
from app.channels.max import max_client
from app.config import settings
from app.db import init_db, session
from app.models import Site

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("korsik")

WEB_DIST = Path(__file__).resolve().parents[2] / "web" / "dist"


def spa_file(root: Path, path: str) -> Path:
    """Файл сборки панели; всё остальное — index.html (маршруты SPA).
    Путь проверяется после resolve: иначе запрос вида /%2e%2e/%2e%2e/.env читает любой файл сервера."""
    f = (root / path).resolve()
    return f if path and f.is_file() and f.is_relative_to(root.resolve()) else root / "index.html"


async def seed_sites() -> None:
    async with session() as s:
        for code, site in scenario()["sites"].items():
            stmt = insert(Site).values(code=code, name=site["name"], kind=site.get("kind", "office"),
                                       tz=site.get("tz", settings().default_tz))
            await s.execute(stmt.on_conflict_do_update(index_elements=[Site.code], set_={"name": stmt.excluded.name, "kind": stmt.excluded.kind}))
        await s.commit()


@asynccontextmanager
async def lifespan(app: FastAPI):
    cfg = settings()
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    await init_db()
    await seed_sites()
    tasks = [asyncio.create_task(scheduler.loop())]
    client = max_client()
    if client and cfg.max_webhook_url:
        await client.subscribe(cfg.max_webhook_url, cfg.max_webhook_secret)
        log.info("MAX: webhook %s", cfg.max_webhook_url)
    elif client:
        tasks.append(asyncio.create_task(poll_loop()))
        log.info("MAX: long polling (dev)")
    else:
        log.info("MAX_TOKEN не задан — работает демо-канал в веб-панели")
    yield
    for t in tasks:
        t.cancel()


app = FastAPI(title="Корсик — ИИ-ассистент адаптации", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
app.include_router(admin.router)
app.include_router(admin.manager_router)
app.include_router(demo.router)
app.mount("/media", StaticFiles(directory=settings().media_dir), name="media")


@app.post("/max/webhook")
async def max_webhook(request: Request, x_max_bot_api_secret: str | None = Header(None)):
    secret = settings().max_webhook_secret
    if secret and x_max_bot_api_secret != secret:
        raise HTTPException(403)
    # MAX ждёт 200 быстро и повторяет доставку при ошибке — обрабатываем событие в фоне
    asyncio.create_task(process_max_update(await request.json()))
    return {"ok": True}


@app.get("/health")
async def health():
    return {"ok": True}


if WEB_DIST.exists():
    app.mount("/assets", StaticFiles(directory=WEB_DIST / "assets"), name="assets")

    @app.get("/{path:path}")
    async def spa(path: str):
        if path.startswith("api/"):
            raise HTTPException(404)  # неизвестный адрес API — 404, а не страница панели
        return FileResponse(spa_file(WEB_DIST, path))
