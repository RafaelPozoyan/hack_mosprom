"""Обязательный ответ, очередь и восстановление состояния проверяются на PostgreSQL."""

import asyncio
import os
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app import channels, scheduler
from app.analytics import sentiment
from app.api import admin, demo
from app.bot import dialog, progress, scenario, surveys
from app.db import Base
from app.models import Analysis, Employee, Job, Message, Site, Staff, StepAttachment, SurveyAnswer, Ticket


@pytest_asyncio.fixture
async def db(monkeypatch):
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL нужен для проверки ожидания шагов PostgreSQL")
    schema = "progress_test_" + uuid4().hex
    owner = create_async_engine(url)
    async with owner.begin() as c:
        await c.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_async_engine(url, connect_args={"server_settings": {"search_path": schema}})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as c:
            await c.run_sync(
                lambda conn: Base.metadata.create_all(
                    conn,
                    tables=[
                        Site.__table__,
                        Staff.__table__,
                        Employee.__table__,
                        Job.__table__,
                        StepAttachment.__table__,
                        Ticket.__table__,
                        Message.__table__,
                        SurveyAnswer.__table__,
                        Analysis.__table__,
                    ],
                )
            )

        @asynccontextmanager
        async def session():
            async with factory() as s:
                yield s

        monkeypatch.setattr(scheduler, "session", session)
        monkeypatch.setattr(scheduler, "_job_slots", asyncio.Semaphore(2))
        monkeypatch.setattr(channels, "max_client", lambda: None)
        monkeypatch.setattr(sentiment, "react", AsyncMock())
        monkeypatch.setattr(surveys, "react", AsyncMock())
        async with factory() as s:
            s.add(Site(code="GTP", name="Демо", kind="plant"))
            await s.flush()
            s.add(
                Employee(
                    id=1,
                    full_name="Демо Иван Петров",
                    first_name="Иван",
                    site_code="GTP",
                    start_date=date(2026, 10, 26),
                    probation_end=date(2027, 1, 26),
                    invite_code="demo",
                    status="active",
                    state={},
                )
            )
            await s.commit()
        yield SimpleNamespace(session=factory)
    finally:
        await engine.dispose()
        async with owner.begin() as c:
            await c.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await owner.dispose()


async def add_job(db, ref, **kwargs):
    async with db.session() as s:
        job = Job(employee_id=1, kind="step", ref=ref, run_at=scheduler.utcnow(), **kwargs)
        s.add(job)
        await s.commit()
        return job.id


@pytest.mark.asyncio
@pytest.mark.parametrize("answer", [0, 1])
async def test_required_access_waits_and_both_answers_release_queue(db, answer):
    access = await add_job(db, "d1_access")
    later = await add_job(db, "w2_check")
    information = await add_job(db, "d1_lunch")
    assert await scheduler.run_job(access)
    assert await scheduler.run_job(later)
    assert await scheduler.run_job(information)
    # Другая сессия восстанавливает ожидание без памяти предыдущего обработчика.
    async with db.session() as s:
        emp = await s.get(Employee, 1)
        assert progress.required_step(emp) == "d1_access"
        waiting = await s.get(Job, later)
        original_time = waiting.run_at
        assert waiting.status == "blocked" and waiting.attempts == 0
        assert (await s.get(Job, information)).status == "done"
        card = await admin.employee_card(1, s)
        statuses = {item["step_id"]: item["status"] for item in card["timeline"]}
        assert statuses["d1_access"] == "waiting"
        assert statuses["w2_check"] == "blocked"
        await dialog.scenario_button(s, emp, f"b:d1_access:0:0:{answer}")
        assert progress.required_step(emp) is None
        # Поздняя противоположная кнопка не меняет уже полученный ответ.
        await dialog.scenario_button(s, emp, f"b:d1_access:0:0:{1 - answer}")
        replies = list(await s.scalars(select(SurveyAnswer)))
        assert len(replies) == 1 and replies[0].flagged == bool(answer)
        await s.refresh(waiting)
        assert waiting.status == "pending" and waiting.run_at == original_time
        card = await admin.employee_card(1, s)
        statuses = {item["step_id"]: item["status"] for item in card["timeline"]}
        assert statuses["d1_access"] == "sent"
        assert statuses["w2_check"] == "pending"
    assert await scheduler.run_job(later)
    async with db.session() as s:
        emp = await s.get(Employee, 1)
        assert emp.state["survey"]["id"] == "week2"
        assert progress.required_step(emp) == "w2_check"


@pytest.mark.asyncio
@pytest.mark.parametrize("form", ["ty", "vy"])
async def test_survey_resume_preserves_question_and_completion_releases_wait(db, form):
    async with db.session() as s:
        emp = await s.get(Employee, 1)
        emp.address_form = form
        await scenario.run_step(s, emp, "d3_survey")
        await surveys.answer(s, emp, "day3", "docs", "a")
        assert emp.state["survey"]["q"] == 1
        await scenario.run_step(s, emp, "d3_survey")
        assert emp.state["survey"]["q"] == 1
        reminders = list(await s.scalars(select(Job).where(Job.kind == "reminder")))
        assert len(reminders) == 2
        with pytest.raises(progress.StepBlocked):
            await scenario.run_step(s, emp, "w2_check")
        await surveys.answer(s, emp, "day3", "docs", "a")
        assert emp.state["survey"]["q"] == 1
        await surveys.answer(s, emp, "day3", "team", "a")
        await surveys.answer(s, emp, "day3", "tasks", "a")
        assert progress.required_step(emp) is None and "survey" not in emp.state
        await scenario.run_step(s, emp, "w2_check")
        assert emp.state["survey"]["id"] == "week2"


@pytest.mark.asyncio
async def test_optional_answers_are_preserved_when_next_survey_starts(db):
    async with db.session() as s:
        emp = await s.get(Employee, 1)
        await scenario.run_step(s, emp, "m1w3_survey")
        await surveys.answer(s, emp, "month2", "mood", "a")
        await scenario.run_step(s, emp, "w2_check")
        assert emp.state["survey"]["id"] == "week2"
        old = list(await s.scalars(select(Job).where(Job.ref == "month2")))
        assert old and all(j.status == "cancelled" for j in old)
        assert len(list(await s.scalars(select(SurveyAnswer)))) == 1
        await surveys.answer(s, emp, "month2", "helpers", "done")
        assert emp.state["survey"]["q"] == 0
        await surveys.answer(s, emp, "week2", "tasks_clear", "no")
        answer_count = len(list(await s.scalars(select(SurveyAnswer))))
        await scenario.run_step(s, emp, "w2_check")
        last = await s.scalar(select(Message).order_by(Message.id.desc()).limit(1))
        assert last.payload["buttons"][0][0]["payload"] == "a:skip_comment"
        assert len(list(await s.scalars(select(SurveyAnswer)))) == answer_count
        await surveys.comment(s, emp, None)  # объяснять причину отрицательного ответа необязательно
        assert emp.state["survey"]["q"] == 1
        assert progress.required_step(emp) == "w2_check"


@pytest.mark.asyncio
async def test_legacy_survey_blocks_new_feedback_but_allows_help(db):
    async with db.session() as s:
        emp = await s.get(Employee, 1)
        emp.state = {"survey": {"id": "day3", "q": 0, "selected": []}}
        await s.commit()
        with pytest.raises(progress.StepBlocked):
            await scenario.run_step(s, emp, "d2_self_intro")
        await dialog.command(s, emp, "hr")
        assert emp.state["await"]["kind"] == "relay"
        assert progress.required_step(emp) == "d3_survey"
        await dialog.action(s, emp, "cancel")
        await scenario.run_step(s, emp, "d1_contacts")
        assert emp.state["survey"]["id"] == "day3"
        upload = SimpleNamespace(filename="plan.txt", read=AsyncMock(return_value=b"not accepted"))
        with pytest.raises(HTTPException) as exc:
            await admin.run_step_now(1, "w2_check", upload, s)
        assert exc.value.status_code == 409
        upload.read.assert_not_awaited()


@pytest.mark.asyncio
async def test_demo_keeps_original_date_when_feedback_is_blocked(db, monkeypatch):
    async with db.session() as s:
        await scenario.run_step(s, await s.get(Employee, 1), "d3_survey")
    jid = await add_job(db, "w2_check")
    async with db.session() as s:
        job = await s.get(Job, jid)
        job.run_at = scheduler.utcnow() + timedelta(days=14)
        original_time = job.run_at
        await s.execute(update(Job).where(Job.kind == "reminder").values(status="cancelled"))
        await s.commit()
    finished = asyncio.Event()

    async def run(job_id):
        try:
            await scheduler.run_job(job_id)
        finally:
            finished.set()

    monkeypatch.setattr(demo, "run_job", run)
    async with db.session() as s:
        result = await demo.run_next(1, s)
    await asyncio.wait_for(finished.wait(), timeout=5)
    async with db.session() as s:
        job = await s.get(Job, jid)
        assert job.status == "blocked" and job.attempts == 0
        assert job.run_at == original_time
        assert result["run_at"] == original_time.isoformat()


@pytest.mark.asyncio
async def test_optional_self_intro_does_not_capture_next_survey_text(db):
    async with db.session() as s:
        emp = await s.get(Employee, 1)
        await scenario.run_step(s, emp, "d2_self_intro")
        assert emp.state["await"]["kind"] == "self_intro"
        await scenario.run_step(s, emp, "d3_survey")
        assert "await" not in emp.state
        assert emp.state["survey"]["id"] == "day3"


async def old_queue(db, *refs):
    async with db.session() as s:
        emp = await s.get(Employee, 1)
        emp.start_date = date.today() - timedelta(days=120)
        emp.probation_end = scenario.add_months(emp.start_date, 3)
        dates = {st["id"]: at for st, at in scenario.timeline(emp)}
        jobs = [Job(employee_id=1, kind="step", ref=ref, run_at=dates[ref]) for ref in refs]
        s.add_all(jobs)
        await s.commit()
        return jobs


@pytest.mark.asyncio
@pytest.mark.parametrize("ref", ["m2_congrats", "m1w3_survey", "d2_self_intro"])
async def test_obsolete_optional_step_is_skipped_with_original_date_and_reason(db, ref):
    (job,) = await old_queue(db, ref)
    assert await scheduler.run_job(job.id)
    async with db.session() as s:
        current = await s.get(Job, job.id)
        assert current.status == "skipped" and current.attempts == 0
        assert current.run_at == job.run_at and "следующий опрос" in current.error
        assert not list(await s.scalars(select(Message)))
        card = await admin.employee_card(1, s)
        item = next(item for item in card["timeline"] if item["step_id"] == ref)
        assert item["status"] == "skipped" and item["status_note"] == current.error
        upload = SimpleNamespace(filename="plan.txt", read=AsyncMock(return_value=b"unused"))
        with pytest.raises(HTTPException) as exc:
            await admin.run_step_now(1, ref, upload, s)
        assert exc.value.status_code == 409
        upload.read.assert_not_awaited()


@pytest.mark.asyncio
async def test_overdue_required_steps_stay_in_order_until_answered(db):
    access, day3, week2 = await old_queue(db, "d1_access", "d3_survey", "w2_check")
    async with db.session() as s:
        with pytest.raises(HTTPException) as exc:
            await admin.run_step_now(1, "w2_check", None, s)
        assert exc.value.status_code == 409 and "обязательный шаг" in exc.value.detail
    assert await scheduler.run_job(access.id)
    assert await scheduler.run_job(day3.id)
    assert await scheduler.run_job(week2.id)
    async with db.session() as s:
        assert (await s.get(Job, day3.id)).status == "blocked"
        await dialog.scenario_button(s, await s.get(Employee, 1), "b:d1_access:0:0:0")
    assert not await scheduler.run_job(week2.id)
    assert await scheduler.run_job(day3.id)
    async with db.session() as s:
        emp = await s.get(Employee, 1)
        for qid in ("docs", "team", "tasks"):
            await surveys.answer(s, emp, "day3", qid, "a")
    assert await scheduler.run_job(week2.id)
    async with db.session() as s:
        assert progress.required_step(await s.get(Employee, 1)) == "w2_check"
        for job in (access, day3, week2):
            assert (await s.get(Job, job.id)).run_at == job.run_at


@pytest.mark.asyncio
async def test_failed_required_step_blocks_next_until_successful_manual_retry(db):
    day3, week2 = await old_queue(db, "d3_survey", "w2_check")
    async with db.session() as s:
        failed = await s.get(Job, day3.id)
        failed.status, failed.attempts = "failed", 3
        failed.run_at = scheduler.utcnow() + timedelta(minutes=3)
        await s.commit()
    assert await scheduler.run_job(week2.id)
    async with db.session() as s:
        assert (await s.get(Job, week2.id)).status == "blocked"
        await admin.run_step_now(1, "d3_survey", None, s)
        emp = await s.get(Employee, 1)
        for qid in ("docs", "team", "tasks"):
            await surveys.answer(s, emp, "day3", qid, "a")
        assert (await s.get(Job, day3.id)).status == "failed"  # история ошибки не стирается
    assert await scheduler.run_job(week2.id)


@pytest.mark.asyncio
async def test_partial_optional_answers_and_skip_reason_survive_next_checkpoint(db):
    async with db.session() as s:
        emp = await s.get(Employee, 1)
        await scenario.run_step(s, emp, "m1w3_survey")
        await surveys.answer(s, emp, "month2", "mood", "a")
        await scenario.run_step(s, emp, "final")
        assert len(list(await s.scalars(select(SurveyAnswer)))) == 1
        reminders = list(await s.scalars(select(Job).where(Job.ref == "month2")))
        assert all(j.status == "cancelled" for j in reminders)
        card = await admin.employee_card(1, s)
        item = next(item for item in card["timeline"] if item["step_id"] == "m1w3_survey")
        assert item["status"] == "skipped" and "Полученные ответы сохранены" in item["status_note"]
    async with db.session() as s:
        emp = await s.get(Employee, 1)
        assert emp.state["feedback_checkpoint"] == "final"
        with pytest.raises(HTTPException) as exc:
            await admin.run_step_now(1, "m2_congrats", None, s)
        assert exc.value.status_code == 409  # дата ещё не наступила, но демо уже дошло до итогов
        await scenario.run_step(s, emp, "d1_lunch")
        assert progress.required_step(emp) == "final"


@pytest.mark.asyncio
async def test_demo_reports_skipped_instead_of_sent(db):
    (job,) = await old_queue(db, "m2_congrats")
    async with db.session() as s:
        result = await demo.run_next(1, s)
        assert result["status"] == "skipped"
        assert datetime.fromisoformat(result["run_at"]) == job.run_at
        assert (await s.get(Job, job.id)).status == "skipped"
