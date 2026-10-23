"""Планировщик проактивных рассылок (CRON по таймлайну). Очередь — таблица jobs в Postgres.

Задачи забираются атомарно (UPDATE ... FOR UPDATE SKIP LOCKED), поэтому воркеров можно запускать
сколько угодно: одна задача не уйдёт дважды. Отдельный брокер (Redis/RabbitMQ) не нужен.
"""

import asyncio
import logging
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select, text

from app.config import settings
from app.db import session
from app.models import Employee, Job

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 3
CLAIM_BATCH = 20

CLAIM_SQL = text("""
    UPDATE jobs SET status = 'running', attempts = attempts + 1
    WHERE id IN (
        SELECT id FROM jobs WHERE status = 'pending' AND run_at <= now()
        ORDER BY run_at LIMIT :n FOR UPDATE SKIP LOCKED
    )
    RETURNING id
""")


def utcnow() -> datetime:
    return datetime.now(ZoneInfo("UTC"))


async def run_job(job_id: int) -> None:
    from app.bot.updates import _locks

    async with session() as s:
        job = await s.get(Job, job_id)
        if job.employee_id:
            # шаг таймлайна и ответы сотрудника не должны менять его состояние одновременно
            async with _locks[job.employee_id]:
                await _execute(s, job)
        else:
            await _execute(s, job)


async def _execute(s, job: Job) -> None:
    from app.analytics import digest
    from app.bot import surveys
    from app.bot.scenario import run_step

    job_id = job.id
    emp = await s.get(Employee, job.employee_id) if job.employee_id else None
    try:
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
        job.error = repr(e)[:1000]
        job.status = "failed" if job.attempts >= MAX_ATTEMPTS else "pending"
        job.run_at = utcnow() + timedelta(minutes=job.attempts)
    await s.commit()


async def tick() -> int:
    async with session() as s:
        ids = list(await s.scalars(CLAIM_SQL, {"n": CLAIM_BATCH}))
        await s.commit()
    for job_id in ids:
        await run_job(job_id)
    return len(ids)


async def ensure_digest_job(s, after: datetime | None = None) -> None:
    """Еженедельный дайджест «Настроения в коллективе новичков» (роль 04 кейса)."""
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
    async with session() as s:
        await ensure_digest_job(s)
        # задачи, «зависшие» в running после рестарта процесса, возвращаем в очередь
        await s.execute(text("UPDATE jobs SET status = 'pending' WHERE status = 'running'"))
        await s.commit()
    while True:
        try:
            await tick()
        except Exception:
            log.exception("scheduler tick failed")
        await asyncio.sleep(settings().scheduler_tick_s)
