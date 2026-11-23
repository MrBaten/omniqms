"""Reply generation, human replies, draft approval and delivery."""
from __future__ import annotations

import logging
from datetime import timedelta

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.base import AccountContext, AdapterError, ReplyTarget, SendResult, dm_window
from app.ai import engine
from app.core.crypto import decrypt, encrypt
from app.models import ConnectedAccount, Conversation, Job, Message, User, Workspace
from app.services import registry
from app.services.queue import MAX_ATTEMPTS, enqueue
from app.services.util import as_utc, now_utc, ws_settings

log = logging.getLogger("omniqms.replies")

MAX_REPLY_CHARS = 2000


class RetryLater(Exception):
    """Raised by a job handler to request a retry with backoff."""


class ReplyNotAllowed(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


# --- helpers ---------------------------------------------------------------------------------
def account_context(acct: ConnectedAccount) -> AccountContext:
    return AccountContext(external_id=acct.external_id, access_token=decrypt(acct.access_token_enc) or "",
                          refresh_token=decrypt(acct.refresh_token_enc), token_expires_at=as_utc(acct.token_expires_at),
                          meta=dict(acct.meta or {}))


async def fresh_context(db: Session, acct: ConnectedAccount, adapter) -> AccountContext:
    """Refresh tokens that expire within 5 minutes (if the adapter supports refresh)."""
    ctx = account_context(acct)
    exp = ctx.token_expires_at
    if exp is not None and exp - now_utc() < timedelta(minutes=5):
        try:
            tokens = await adapter.refresh_tokens(ctx)
        except AdapterError as e:
            if e.needs_reconnect:
                acct.status = "needs_reconnect"
                db.commit()
            raise
        if tokens is not None:
            store_tokens(acct, tokens)
            db.commit()
            ctx = account_context(acct)
    return ctx


def store_tokens(acct: ConnectedAccount, tokens) -> None:
    acct.access_token_enc = encrypt(tokens.access_token)
    if tokens.refresh_token:
        acct.refresh_token_enc = encrypt(tokens.refresh_token)
    acct.token_expires_at = tokens.expires_at


def reply_window(conv: Conversation) -> dict:
    if conv.kind != "dm":
        return {"dm_ai_allowed": True, "dm_human_allowed": True, "expires_at": None}
    last = as_utc(conv.last_customer_message_at)
    ai, human = dm_window(last)
    expires = None
    if last is not None:
        if ai:
            expires = last + timedelta(hours=24)
        elif human:
            expires = last + timedelta(days=7)
    return {"dm_ai_allowed": ai, "dm_human_allowed": human, "expires_at": expires}


def _latest_inbound(db: Session, conv: Conversation) -> Message | None:
    return db.scalar(select(Message).where(Message.conversation_id == conv.id, Message.direction == "inbound")
                     .order_by(Message.id.desc()).limit(1))


def _history(db: Session, conv: Conversation, before_id: int, limit: int = 10) -> list[dict]:
    rows = list(db.scalars(select(Message).where(Message.conversation_id == conv.id, Message.id < before_id,
                                                 Message.delivery_status != "draft")
                           .order_by(Message.id.desc()).limit(limit)))
    return [{"author": m.author, "text": m.text} for m in reversed(rows)]


# --- job: generate_reply ---------------------------------------------------------------------
async def handle_generate_reply(db: Session, job: Job) -> None:
    msg = db.get(Message, (job.payload or {}).get("message_id"))
    if msg is None or msg.direction != "inbound":
        return
    conv = db.get(Conversation, msg.conversation_id)
    ws = db.get(Workspace, msg.workspace_id)
    acct = db.get(ConnectedAccount, msg.account_id)
    if conv is None or ws is None or acct is None:
        return
    # Only answer the latest customer message; skip if anything was already sent/drafted after it.
    later = db.scalar(select(Message.id).where(Message.conversation_id == conv.id, Message.id > msg.id).limit(1))
    if later is not None:
        return
    settings = ws_settings(ws)
    if not settings.get("auto_reply_enabled", True):
        if conv.status != "pending_human":
            conv.status = "open"
        db.commit()
        return
    if acct.status != "connected":
        conv.status = "pending_human"
        db.commit()
        return

    result = await engine.answer(db, ws, msg.text, _history(db, conv, msg.id), kind=conv.kind)
    threshold = float(settings.get("confidence_threshold", 0.6))
    ai_allowed = reply_window(conv)["dm_ai_allowed"]
    confident = (not result["needs_human"]) and result["answer"] and result["confidence"] >= threshold
    if confident and ai_allowed:
        out = Message(workspace_id=ws.id, conversation_id=conv.id, account_id=acct.id, direction="outbound",
                      author="ai", text=result["answer"], ai_confidence=result["confidence"],
                      ai_sources=result["sources"], delivery_status="queued")
        db.add(out)
        db.flush()
        conv.status = "auto_replied"
        enqueue(db, "send_reply", {"message_id": out.id}, workspace_id=ws.id)
    else:
        if result["answer"]:
            db.add(Message(workspace_id=ws.id, conversation_id=conv.id, account_id=acct.id, direction="outbound",
                           author="ai", text=result["answer"], ai_confidence=result["confidence"],
                           ai_sources=result["sources"], delivery_status="draft",
                           error=None if ai_allowed else "outside_ai_window"))
        conv.status = "pending_human"
    conv.updated_at = now_utc()
    db.commit()


# --- human actions ---------------------------------------------------------------------------
def _check_human_can_reply(conv: Conversation, acct: ConnectedAccount) -> None:
    if acct.status != "connected":
        raise ReplyNotAllowed("account_not_connected")
    if conv.kind == "dm" and not reply_window(conv)["dm_human_allowed"]:
        raise ReplyNotAllowed("outside_messaging_window")


def create_human_reply(db: Session, conv: Conversation, user: User, text: str, resolve: bool = True) -> Message:
    acct = db.get(ConnectedAccount, conv.account_id)
    _check_human_can_reply(conv, acct)
    out = Message(workspace_id=conv.workspace_id, conversation_id=conv.id, account_id=conv.account_id,
                  direction="outbound", author="agent", author_user_id=user.id, text=text[:MAX_REPLY_CHARS],
                  delivery_status="queued")
    db.add(out)
    db.flush()
    enqueue(db, "send_reply", {"message_id": out.id}, workspace_id=conv.workspace_id)
    conv.status = "resolved" if resolve else "open"
    conv.assigned_user_id = user.id
    conv.updated_at = now_utc()
    db.commit()
    return out


def approve_draft(db: Session, conv: Conversation, draft: Message, user: User, text: str | None = None) -> Message:
    if draft.conversation_id != conv.id or draft.delivery_status != "draft" or draft.direction != "outbound":
        raise ReplyNotAllowed("not_a_draft")
    acct = db.get(ConnectedAccount, conv.account_id)
    _check_human_can_reply(conv, acct)
    if text is not None and text.strip() and text.strip() != draft.text:
        draft.text = text.strip()[:MAX_REPLY_CHARS]
        draft.author = "agent"
    draft.author_user_id = user.id  # human reviewed → human-sent for window rules
    draft.delivery_status = "queued"
    draft.error = None
    db.flush()
    enqueue(db, "send_reply", {"message_id": draft.id}, workspace_id=conv.workspace_id)
    conv.status = "resolved"
    conv.assigned_user_id = user.id
    conv.updated_at = now_utc()
    db.commit()
    return draft


# --- job: send_reply -------------------------------------------------------------------------
async def handle_send_reply(db: Session, job: Job) -> None:
    out = db.get(Message, (job.payload or {}).get("message_id"))
    if out is None or out.direction != "outbound" or out.delivery_status != "queued":
        return
    conv = db.get(Conversation, out.conversation_id)
    acct = db.get(ConnectedAccount, out.account_id)
    if conv is None or acct is None:
        return

    def fail(error: str, *, reconnect: bool = False) -> None:
        log.warning("send_reply failed: platform=%s account=%s message=%s error=%s reconnect=%s",
                    acct.platform, acct.id, out.id, error[:200], reconnect)
        out.delivery_status = "failed"
        out.error = error[:500]
        conv.status = "pending_human"
        if reconnect:
            acct.status = "needs_reconnect"
        db.commit()

    if acct.status != "connected":
        return fail("account_not_connected")
    adapter = registry.find_adapter(acct.platform)
    if adapter is None:
        return fail("unknown_platform")

    human = out.author_user_id is not None or out.author == "agent"
    if conv.kind == "dm":
        ai_ok, human_ok = dm_window(as_utc(conv.last_customer_message_at))
        if not (human_ok if human else ai_ok):
            return fail("outside_messaging_window")

    latest = _latest_inbound(db, conv)
    target = ReplyTarget(kind=conv.kind, thread_external_id=conv.external_thread_id,
                         last_customer_message_at=as_utc(conv.last_customer_message_at), sent_by_human=human,
                         reply_to_external_message_id=latest.external_id if latest else None,
                         parent_external_id=conv.parent_external_id)
    try:
        ctx = await fresh_context(db, acct, adapter)
        res: SendResult = await adapter.send_reply(ctx, target, out.text)
    except AdapterError as e:
        res = SendResult(ok=False, error=str(e) or "adapter_error", retryable=e.retryable,
                         needs_reconnect=e.needs_reconnect)
    except httpx.HTTPError as e:
        res = SendResult(ok=False, error=f"network_error:{type(e).__name__}", retryable=True)

    if res.ok:
        out.delivery_status = "sent"
        out.external_id = res.external_message_id
        out.error = None
        db.commit()
        log.info("send_reply ok: platform=%s account=%s message=%s kind=%s author=%s",
                 acct.platform, acct.id, out.id, conv.kind, out.author)
        return
    if res.needs_reconnect:
        return fail(res.error or "needs_reconnect", reconnect=True)
    if res.retryable and job.attempts < MAX_ATTEMPTS:
        out.error = (res.error or "retrying")[:500]
        db.commit()
        raise RetryLater(res.error or "retryable send failure")
    return fail(res.error or "send_failed")
