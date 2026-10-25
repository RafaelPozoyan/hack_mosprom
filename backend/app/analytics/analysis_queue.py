"""Устойчивый разбор ответов: LLM не удерживает диалог, применение результата атомарно."""

import asyncio
import logging
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.analytics import sentiment
from app.db import session
from app.models import AnalysisTask, Employee, Question
from app.scheduler import MAX_ATTEMPTS, utcnow

log = logging.getLogger(__name__)
LOCK_NAMESPACE = 0x414E4C59  # ANLY: независимо от блокировок заданий и диалогов
POLL_INTERVAL_S = 1  # один фоновый разбор за раз, без активного ожидания


async def enqueue(s, emp, text: str, source: str, source_id: int, extra_flags=None) -> None:
    """Вызывается до commit ответа; повтор одного Question/SurveyAnswer не создаёт вторую задачу."""
    await s.execute(
        insert(AnalysisTask)
        .values(
            employee_id=emp.id,
            source=source,
            source_id=source_id,
            payload={
                "text": text,
                "context": await sentiment.last_bot_text(s, emp),
                "extra_flags": extra_flags,
            },
            available_at=utcnow(),
        )
        .on_conflict_do_nothing(index_elements=[AnalysisTask.source, AnalysisTask.source_id])
    )


async def _retry(task_id: int, error: Exception) -> None:
    async with session() as s, s.begin():
        row = await s.get(AnalysisTask, task_id)
        if row:
            row.status = "failed" if row.attempts >= MAX_ATTEMPTS else "pending"
            row.error = type(error).__name__  # без текста сотрудника и ответа модели
            row.available_at = utcnow() + timedelta(minutes=row.attempts)


async def _apply(task_id: int, employee_id: int, res, error: str | None) -> None:
    from app.bot.updates import _locks

    callbacks = []
    async with _locks[employee_id], session() as s, s.begin():
        # DELETE сотрудника берёт родительскую строку перед каскадом задач: сохраняем тот же порядок.
        if (
            await s.scalar(select(Employee).where(Employee.id == employee_id).with_for_update(of=Employee))
            is None
        ):
            return
        row = await s.get(AnalysisTask, task_id, with_for_update=True)
        if row is None or row.status == "done":
            return  # удаление сотрудника каскадно удаляет его разборы
        connection = await s.connection()
        async with (
            connection.begin_nested(),
            AsyncSession(
                bind=connection, join_transaction_mode="rollback_only", expire_on_commit=False
            ) as work,
        ):
            work.info["after_commit"] = callbacks
            emp = await work.get(Employee, employee_id)
            if emp:
                payload = row.payload
                result = await sentiment.save_and_react(
                    work, emp, res, payload["text"], row.source, payload.get("extra_flags")
                )
                if result and row.source == "chat":
                    question = await work.get(Question, row.source_id)
                    if question:
                        question.topic = result.topics[0]
                await work.flush()
        row.status, row.error, row.payload = "done", error, {}
    for callback in callbacks:
        try:
            callback()
        except Exception:
            log.exception("analysis post-commit callback failed")


async def _process(task_id: int) -> None:
    async with session() as s, s.begin():
        row = await s.get(AnalysisTask, task_id, with_for_update=True)
        if row is None or row.status not in ("pending", "running") or row.available_at > utcnow():
            return
        run_model = row.attempts < MAX_ATTEMPTS
        row.attempts += int(run_model)
        row.status = "running"
        row.available_at = utcnow() + timedelta(minutes=row.attempts)
    # Счётчик переживает аварийное завершение; во время LLM строки БД и диалог не заблокированы.
    res, error = None, row.error or (None if run_model else "Interrupted")
    if run_model:
        try:
            res = await sentiment.analyze_text(row.payload["text"], context=row.payload["context"])
        except Exception as exc:
            if row.attempts < MAX_ATTEMPTS and not row.payload.get("extra_flags"):
                await _retry(task_id, exc)
                return
            error = type(exc).__name__
        else:
            error = None
    # Явный флаг не ждёт повторов LLM: прежняя резервная реакция нужна уже при первой ошибке.
    await _apply(task_id, row.employee_id, res, error)


async def process_one() -> bool:
    async with session() as guard, guard.begin():
        ids = list(
            await guard.scalars(
                select(AnalysisTask.id)
                .where(AnalysisTask.status.in_(("pending", "running")), AnalysisTask.available_at <= utcnow())
                .order_by(AnalysisTask.id)
                .limit(20)  # как у планировщика: ограниченная пачка кандидатов
            )
        )
        for task_id in ids:
            if not await guard.scalar(select(func.pg_try_advisory_xact_lock(LOCK_NAMESPACE, task_id))):
                continue
            try:
                await _process(task_id)
            except Exception as exc:
                await _retry(task_id, exc)
                log.warning("analysis task=%s error=%s", task_id, type(exc).__name__)
            return True
    return False


async def loop() -> None:
    while True:
        try:
            if await process_one():
                continue
        except Exception:
            log.exception("analysis worker failed")
        await asyncio.sleep(POLL_INTERVAL_S)
