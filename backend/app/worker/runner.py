"""In-process async worker: processes due jobs and schedules polls."""
from __future__ import annotations

import asyncio
import logging
import time

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.db import SessionLocal
from app.services import polling, replies
from app.services.queue import claim_next, mark_done, mark_failed

log = logging.getLogger("omniqms.worker")

HANDLERS = {
    "generate_reply": replies.handle_generate_reply,
    "send_reply": replies.handle_send_reply,
    "poll_account": polling.handle_poll_account,
    "refresh_tokens": polling.handle_refresh_tokens,
}


async def process_due_jobs(db: Session | None = None, max_jobs: int = 50) -> int:
    """Process up to max_jobs due jobs. Returns number processed."""
    own = db is None
    db = db or SessionLocal()
    done = 0
    try:
        for _ in range(max_jobs):
            job = claim_next(db)
            if job is None:
                break
            handler = HANDLERS.get(job.type)
            if handler is None:
                mark_failed(db, job, f"unknown job type {job.type}", retry=False)
                continue
            try:
                await handler(db, job)
                mark_done(db, job)
            except replies.RetryLater as e:
                db.rollback()
                mark_failed(db, job, str(e), retry=True)
            except Exception as e:  # keep the worker alive
                db.rollback()
                log.exception("job %s (%s) failed", job.id, job.type)
                mark_failed(db, job, f"{type(e).__name__}: {e}", retry=True)
            done += 1
    finally:
        if own:
            db.close()
    return done


async def run_worker(stop_event: asyncio.Event) -> None:
    s = get_settings()
    last_schedule = 0.0
    last_refresh = 0.0
    log.info("worker started")
    while not stop_event.is_set():
        processed = 0
        db = SessionLocal()
        try:
            if time.monotonic() - last_schedule > 15:
                polling.schedule_polls(db)
                if time.monotonic() - last_refresh > 3600:
                    polling.schedule_token_refreshes(db)
                    last_refresh = time.monotonic()
                last_schedule = time.monotonic()
            processed = await process_due_jobs(db)
        except Exception:
            log.exception("worker loop error")
        finally:
            db.close()
        if processed == 0:
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=s.worker_interval_seconds)
            except asyncio.TimeoutError:
                pass
    log.info("worker stopped")
