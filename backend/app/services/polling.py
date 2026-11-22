"""Polling for platforms without webhooks (YouTube)."""
from __future__ import annotations

import logging
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.base import AdapterError
from app.core.config import get_settings
from app.models import ConnectedAccount, Job
from app.services import registry
from app.services.ingest import ingest_event
from app.services.queue import enqueue
from app.services.replies import fresh_context, store_tokens
from app.services.util import as_utc, now_utc

log = logging.getLogger("omniqms.polling")


def schedule_polls(db: Session) -> int:
    """Enqueue poll_account jobs for due accounts on polling platforms. Returns jobs created."""
    try:
        polling = {a.platform for a in registry.all_adapters() if not a.uses_webhooks}
    except Exception as e:  # registry not ready
        log.warning("adapter registry unavailable: %s", type(e).__name__)
        return 0
    if not polling:
        return 0
    interval = timedelta(seconds=get_settings().youtube_poll_seconds)
    now = now_utc()
    pending = {(j.payload or {}).get("account_id") for j in
               db.scalars(select(Job).where(Job.type == "poll_account", Job.status.in_(("pending", "running"))))}
    created = 0
    for acct in db.scalars(select(ConnectedAccount).where(ConnectedAccount.platform.in_(polling),
                                                          ConnectedAccount.status == "connected")):
        last = as_utc(acct.last_polled_at)
        if acct.id in pending or (last is not None and now - last < interval):
            continue
        enqueue(db, "poll_account", {"account_id": acct.id}, workspace_id=acct.workspace_id)
        created += 1
    db.commit()
    return created


async def handle_poll_account(db: Session, job: Job) -> None:
    acct = db.get(ConnectedAccount, (job.payload or {}).get("account_id"))
    if acct is None or acct.status != "connected":
        return
    adapter = registry.find_adapter(acct.platform)
    if adapter is None:
        return
    since = as_utc(acct.last_polled_at)
    try:
        ctx = await fresh_context(db, acct, adapter)
        res = await adapter.poll(ctx, since)
    except AdapterError as e:
        acct.last_polled_at = now_utc()
        if e.needs_reconnect:
            acct.status = "needs_reconnect"
        acct.meta = {**(acct.meta or {}), "last_error": str(e)[:300]}
        db.commit()
        if e.retryable:
            raise
        return
    if res.tokens is not None:
        store_tokens(acct, res.tokens)
    if res.meta_updates:
        acct.meta = {**(acct.meta or {}), **res.meta_updates}
    if res.needs_reconnect:
        acct.status = "needs_reconnect"
    acct.last_polled_at = now_utc()
    db.commit()
    for ev in res.events:
        ingest_event(db, ev)
