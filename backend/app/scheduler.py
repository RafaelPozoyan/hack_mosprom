"""Очередь рассылок в PostgreSQL. Владение задачей удерживается отдельной транзакцией.

Бизнес-обработчики могут делать commit, не освобождая блокировку задачи. После аварии
running без владельца можно подобрать снова. Исходящие отправки пока не атомарны с БД.
"""

import asyncio
import logging
from contextlib import nullcontext
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, or_, select

from app.config import settings
from app.db import session
from app.models import Employee, Job

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 3
CANDIDATE_BATCH = 20

# Отдельные пространства ключей; id=0 зарезервирован для создания следующего дайджеста.
JOB_LOCK_NAMESPACE = 0x4B4F5253  # KORS
EMPLOYEE_LOCK_NAMESPACE = JOB_LOCK_NAMESPACE + 1
_job_slots = asyncio.Semaphore(2)  # каждая задача занимает соединение для блокировки и для бизнес-операций


def utcnow() -> datetime:
    return datetime.now(ZoneInfo("UTC"))


async def run_job(job_id: int) -> bool:
    from app.bot.updates import _locks

    async with _job_slots, session() as guard, guard.begin():
        if not await guard.scalar(select(func.pg_try_advisory_xact_lock(JOB_LOCK_NAMESPACE, job_id))):
            return False
        async with session() as s:
            job = await s.get(Job, job_id)
            if job is None:
                return False
            if job.employee_id and not await guard.scalar(select(
                func.pg_try_advisory_xact_lock(EMPLOYEE_LOCK_NAMESPACE, job.employee_id)
            )):
                return False
            # Сначала локальная блокировка диалога: он тоже меняет jobs и может делать commit.
            async with _locks[job.employee_id] if job.employee_id else nullcontext():
                job = await s.scalar(select(Job).where(Job.id == job_id).with_for_update()
                                     .execution_options(populate_existing=True))
                if job is None or job.status not in ("pending", "running"):
                    return False
                if job.status == "pending" and job.run_at > utcnow():
                    return False
                if job.employee_id and await s.scalar(select(Job.id).where(
                    Job.employee_id == job.employee_id,
                    or_((Job.status == "pending") & (Job.run_at <= func.now()), Job.status == "running"),
                    or_(Job.run_at < job.run_at, (Job.run_at == job.run_at) & (Job.id < job.id)),
                ).limit(1)):
                    return False
                if job.attempts >= MAX_ATTEMPTS:
                    await _retry(s, job, "Обработка прервана до завершения последней попытки")
                    return True
                job.status, job.attempts = "running", job.attempts + 1
                await s.commit()
                await _execute(s, job)
                return True


async def _retry(s, job: Job, error: str) -> None:
    job.error = error[:1000]
    job.status = "failed" if job.attempts >= MAX_ATTEMPTS else "pending"
    job.run_at = utcnow() + timedelta(minutes=job.attempts)
    if job.kind == "digest" and job.status == "failed":
        await ensure_digest_job(s, after=utcnow())
    await s.commit()


async def _execute(s, job: Job) -> None:
    from app.analytics import digest
    from app.bot import surveys
    from app.bot.scenario import run_step

    job_id = job.id
    try:
        emp = await s.get(Employee, job.employee_id) if job.employee_id else None
        if job.kind == "step" and emp:
            await run_step(s, emp, job.ref)
        elif job.kind == "reminder" and emp:
            await surveys.remind(s, emp, job.ref)
        elif job.kind == "digest":
            await digest.run(s)
            await ensure_digest_job(s, after=utcnow())
        job.status, job.done_at, job.error = "done", utcnow(), None
    except Exception as e:
        log.exception("job %s failed", job_id)
        await s.rollback()
        job = await s.get(Job, job_id)
        if job is not None:
            await _retry(s, job, repr(e))
        return
    await s.commit()


async def tick() -> int:
    async with session() as s:
        # Выбираем кандидатов, не захватываем пачку: авария не оставит остальные задачи running.
        ids = list(await s.scalars(select(Job.id).where(or_(
            (Job.status == "pending") & (Job.run_at <= func.now()), Job.status == "running",
        )).order_by(Job.run_at, Job.id).limit(CANDIDATE_BATCH)))
    executed = 0
    for job_id in ids:
        try:
            executed += await run_job(job_id)
        except Exception:
            log.exception("job %s crashed", job_id)
    return executed


async def ensure_digest_job(s, after: datetime | None = None) -> None:
    """Еженедельный дайджест «Настроения в коллективе новичков» (роль 04 кейса)."""
    await s.execute(select(func.pg_advisory_xact_lock(JOB_LOCK_NAMESPACE, 0)))
    exists = await s.scalar(select(Job.id).where(Job.kind == "digest", Job.status == "pending"))
    if exists:
        return
    cfg = settings()
    tz = ZoneInfo(cfg.default_tz)
    now = (after or utcnow()).astimezone(tz)
    days = (cfg.digest_weekday - now.weekday()) % 7 or 7
    hh, mm = map(int, cfg.digest_time.split(":"))
    run_at = datetime.combine(now.date() + timedelta(days=days), time(hh, mm), tzinfo=tz)
    s.add(Job(kind="digest", ref="weekly", run_at=run_at))
    await s.commit()


async def loop() -> None:
    while True:
        try:
            async with session() as s:
                await ensure_digest_job(s)
            await tick()
        except Exception:
            log.exception("scheduler tick failed")
        await asyncio.sleep(settings().scheduler_tick_s)
