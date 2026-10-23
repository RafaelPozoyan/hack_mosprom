from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import settings

engine = create_async_engine(settings().database_url, pool_size=10, max_overflow=10)
Session = async_sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


@asynccontextmanager
async def session() -> AsyncIterator[AsyncSession]:
    async with Session() as s:
        yield s


async def get_session() -> AsyncIterator[AsyncSession]:
    async with Session() as s:
        yield s


async def init_db() -> None:
    from app import models  # noqa: F401  — регистрирует таблицы в metadata

    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await conn.run_sync(Base.metadata.create_all)
