"""DB-backed job queue."""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.models import Job
from app.services.util import now_utc

MAX_ATTEMPTS = 5
STALE_RUNNING = timedelta(minutes=10)


def enqueue(db: Session, type_: str, payload: dict, workspace_id: int | None = None,
            delay_seconds: float = 0) -> Job:
    job = Job(type=type_, payload=payload, workspace_id=workspace_id,
              run_after=now_utc() + timedelta(seconds=delay_seconds))
    db.add(job)
    db.flush()
    return job


def backoff_seconds(attempts: int) -> float:
    return min(5 * (2 ** max(attempts - 1, 0)), 600)


def _requeue_stale(db: Session) -> None:
    cutoff = now_utc() - STALE_RUNNING
    db.execute(update(Job).where(Job.status == "running", Job.run_after < cutoff)
               .values(status="pending"))


def claim_next(db: Session) -> Job | None:
    """Claim one due job. Uses SKIP LOCKED on Postgres; single worker assumed on SQLite."""
    _requeue_stale(db)
    q = (select(Job).where(Job.status == "pending", Job.run_after <= now_utc())
         .order_by(Job.run_after, Job.id).limit(1))
    if db.get_bind().dialect.name == "postgresql":
        q = q.with_for_update(skip_locked=True)
    job = db.scalar(q)
    if job is None:
        db.commit()
        return None
    job.status = "running"
    job.attempts += 1
    job.run_after = now_utc()  # marks claim time for stale detection
    db.commit()
    return job


def mark_done(db: Session, job: Job) -> None:
    job.status = "done"
    job.last_error = None
    db.commit()


def mark_failed(db: Session, job: Job, error: str, retry: bool = True) -> None:
    job.last_error = (error or "")[:2000]
    if retry and job.attempts < MAX_ATTEMPTS:
        job.status = "pending"
        job.run_after = now_utc() + timedelta(seconds=backoff_seconds(job.attempts))
    else:
        job.status = "failed"
    db.commit()
