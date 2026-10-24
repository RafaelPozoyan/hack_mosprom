from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from functools import partial

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import settings

engine = create_async_engine(settings().database_url, pool_size=10, max_overflow=10)
Session = async_sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def after_commit(s: AsyncSession, callback: Callable, *args, **kwargs) -> None:
    """После локального commit выполнить действие или отложить до commit входящего события."""
    pending = s.info.get("after_commit")
    if pending is None:
        callback(*args, **kwargs)
    else:
        pending.append(partial(callback, *args, **kwargs))


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
        # create_all не добавляет колонки в существующие таблицы, а миграций в проекте нет
        await conn.execute(
            text("ALTER TABLE employees ADD COLUMN IF NOT EXISTS personnel_number VARCHAR(50)")
        )
        await conn.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS ix_employees_personnel_number ON employees (personnel_number)"
            )
        )
