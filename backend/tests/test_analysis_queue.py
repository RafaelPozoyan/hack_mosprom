"""Аварии анализа не теряют ответы и не дублируют реакции HR."""

import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import delete, func, select, text

from app.analytics import analysis_queue as queue
from app.analytics import sentiment
from app.bot import updates
from app.models import Analysis, AnalysisTask, Employee, Question, Ticket
from app.scheduler import MAX_ATTEMPTS, utcnow
from tests import test_inbox_atomic

db = test_inbox_atomic.db


async def enqueue(db, flags=None):
    async with db.session() as s:
        emp = await s.get(Employee, 1)
        question = Question(employee_id=1, text="Не понимаю задачи")
        s.add(question)
        await s.flush()
        await queue.enqueue(s, emp, question.text, "chat", question.id, flags)
        await queue.enqueue(s, emp, question.text, "chat", question.id, flags)
        await s.commit()
        assert await s.scalar(select(func.count()).select_from(AnalysisTask)) == 1
        return question.id


async def ready(db):
    async with db.session() as s:
        task = await s.scalar(select(AnalysisTask))
        task.available_at = utcnow() - timedelta(seconds=1)
        await s.commit()


def result(severity="low"):
    return sentiment.TextAnalysis(
        sentiment="negative",
        emotions=[],
        topics=["другое"],
        risk_flags=[],
        severity=severity,
        summary="Сложности",
    )


@pytest.mark.asyncio
async def test_cancelled_analysis_resumes_without_holding_employee_lock(db, monkeypatch):
    question_id = await enqueue(db)
    entered = asyncio.Event()

    async def stalled(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(sentiment, "analyze_text", stalled)
    worker = asyncio.create_task(queue.process_one())
    try:
        await asyncio.wait_for(entered.wait(), 5)
        assert not await queue.process_one()  # второй воркер не обрабатывает тот же ответ
        async with asyncio.timeout(5), updates._locks[1], db.session() as s:
            emp = await s.get(Employee, 1)
            emp.state = {"new_answer": True}
            await s.commit()
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
    await ready(db)
    monkeypatch.setattr(sentiment, "analyze_text", AsyncMock(return_value=result()))
    assert await queue.process_one()
    assert not await queue.process_one()
    async with db.session() as s:
        assert await s.scalar(select(func.count()).select_from(Analysis)) == 1
        assert (await s.scalar(select(AnalysisTask))).status == "done"
        assert (await s.get(Question, question_id)).topic == "другое"
        assert (await s.get(Employee, 1)).state == {"new_answer": True}


@pytest.mark.asyncio
async def test_failed_apply_rolls_back_analysis_and_alert_before_retry(db, monkeypatch):
    await enqueue(db)
    monkeypatch.setattr(sentiment, "analyze_text", AsyncMock(return_value=result("high")))
    apply = sentiment.save_and_react

    async def interrupted(*args):
        await apply(*args)
        raise RuntimeError("after reaction, before queue commit")

    monkeypatch.setattr(sentiment, "save_and_react", interrupted)
    assert await queue.process_one()
    async with db.session() as s:
        assert await s.scalar(select(func.count()).select_from(Analysis)) == 0
        assert await s.scalar(select(func.count()).select_from(Ticket)) == 0
        assert (await s.scalar(select(AnalysisTask))).status == "pending"
    await ready(db)
    monkeypatch.setattr(sentiment, "save_and_react", apply)
    assert await queue.process_one()
    assert not await queue.process_one()
    async with db.session() as s:
        assert await s.scalar(select(func.count()).select_from(Analysis)) == 1
        assert await s.scalar(select(func.count()).select_from(Ticket)) == 1


@pytest.mark.asyncio
async def test_exhausted_model_retries_still_apply_explicit_flags_once(db, monkeypatch):
    await enqueue(db, ["намерение уйти"])
    analyze = AsyncMock(side_effect=RuntimeError("model unavailable"))
    monkeypatch.setattr(sentiment, "analyze_text", analyze)
    for _ in range(MAX_ATTEMPTS):
        await ready(db)
        assert await queue.process_one()
    assert not await queue.process_one()
    assert analyze.await_count == MAX_ATTEMPTS
    async with db.session() as s:
        assert await s.scalar(select(func.count()).select_from(Analysis)) == 0
        assert await s.scalar(select(func.count()).select_from(Ticket)) == 1
        assert (await s.scalar(select(AnalysisTask))).status == "done"


@pytest.mark.asyncio
@pytest.mark.parametrize("during_model", [False, True])
async def test_deleted_employee_cancels_analysis_without_reaction(db, monkeypatch, during_model):
    await enqueue(db)

    async def remove():
        async with db.session() as s:
            await s.execute(delete(Employee).where(Employee.id == 1))
            await s.commit()

    async def analyze(*args, **kwargs):
        await asyncio.wait_for(remove(), 5)
        return result("high")

    model = AsyncMock(side_effect=analyze)
    monkeypatch.setattr(sentiment, "analyze_text", model)
    if not during_model:
        await remove()
    assert await queue.process_one() is during_model
    assert model.await_count == int(during_model)
    async with db.session() as s:
        assert await s.scalar(select(func.count()).select_from(AnalysisTask)) == 0
        assert await s.scalar(select(func.count()).select_from(Analysis)) == 0
        assert await s.scalar(select(func.count()).select_from(Ticket)) == 0


@pytest.mark.asyncio
async def test_delete_during_apply_uses_same_parent_lock_order(db, monkeypatch):
    await enqueue(db)
    monkeypatch.setattr(sentiment, "analyze_text", AsyncMock(return_value=result("high")))
    entered, release, deleting = asyncio.Event(), asyncio.Event(), asyncio.Event()
    apply = sentiment.save_and_react
    retry = AsyncMock(wraps=queue._retry)
    monkeypatch.setattr(queue, "_retry", retry)
    pid = None

    async def paused(*args):
        entered.set()
        await release.wait()
        return await apply(*args)

    async def remove():
        nonlocal pid
        async with db.session() as s:
            pid = await s.scalar(select(func.pg_backend_pid()))
            deleting.set()
            await s.execute(delete(Employee).where(Employee.id == 1))
            await s.commit()

    async def wait_for_database_lock():
        async with db.session() as s:
            while (
                await s.scalar(
                    text("SELECT wait_event_type FROM pg_stat_activity WHERE pid = :pid"), {"pid": pid}
                )
                != "Lock"
            ):
                await s.rollback()  # новый снимок состояния соединения, без задержек наугад

    monkeypatch.setattr(sentiment, "save_and_react", paused)
    worker, deletion = asyncio.create_task(queue.process_one()), None
    try:
        await asyncio.wait_for(entered.wait(), 5)
        deletion = asyncio.create_task(remove())
        await asyncio.wait_for(deleting.wait(), 5)
        await asyncio.wait_for(wait_for_database_lock(), 5)
        release.set()
        await asyncio.wait_for(asyncio.gather(worker, deletion), 5)
    finally:
        release.set()
        tasks = [task for task in (worker, deletion) if task]
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    retry.assert_not_awaited()
    async with db.session() as s:
        assert await s.scalar(select(func.count()).select_from(AnalysisTask)) == 0
        assert await s.scalar(select(func.count()).select_from(Analysis)) == 0
