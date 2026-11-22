"""Normalised inbound events → contacts, conversations, messages, reply jobs."""
from __future__ import annotations

import logging
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.adapters.base import InboundEvent
from app.models import ConnectedAccount, Contact, Conversation, Message
from app.services.queue import enqueue
from app.services.util import as_utc, now_utc

log = logging.getLogger("omniqms.ingest")

MAX_TEXT = 5000


def ingest_event(db: Session, event: InboundEvent) -> Message | None:
    """Store one inbound event. Idempotent; returns the new Message or None if dropped."""
    acct = db.scalar(select(ConnectedAccount).where(ConnectedAccount.platform == event.platform,
                                                    ConnectedAccount.external_id == str(event.account_external_id)))
    if acct is None or acct.status == "disconnected":
        return None
    if event.is_echo or str(event.author_external_id) == acct.external_id:
        return None
    text = (event.text or "").strip()[:MAX_TEXT]
    if not text or not event.external_message_id:
        return None

    existing = db.scalar(select(Message.id).where(Message.account_id == acct.id,
                                                  Message.external_id == str(event.external_message_id)))
    if existing is not None:
        return None

    now = now_utc()
    ts = as_utc(event.timestamp) or now
    if ts > now + timedelta(minutes=5):  # never trust far-future timestamps
        ts = now

    try:
        contact = db.scalar(select(Contact).where(Contact.workspace_id == acct.workspace_id,
                                                  Contact.platform == acct.platform,
                                                  Contact.external_user_id == str(event.author_external_id)))
        if contact is None:
            contact = Contact(workspace_id=acct.workspace_id, platform=acct.platform,
                              external_user_id=str(event.author_external_id),
                              display_name=(event.author_name or "")[:300])
            db.add(contact)
            db.flush()
        elif event.author_name and contact.display_name != event.author_name[:300]:
            contact.display_name = event.author_name[:300]

        conv = db.scalar(select(Conversation).where(Conversation.account_id == acct.id,
                                                    Conversation.kind == event.kind,
                                                    Conversation.external_thread_id == str(event.thread_external_id)))
        if conv is None:
            conv = Conversation(workspace_id=acct.workspace_id, account_id=acct.id, contact_id=contact.id,
                                platform=acct.platform, kind=event.kind,
                                external_thread_id=str(event.thread_external_id),
                                parent_external_id=(str(event.parent_external_id) if event.parent_external_id else None),
                                status="open")
            db.add(conv)
            db.flush()
        last = as_utc(conv.last_customer_message_at)
        if last is None or ts > last:
            conv.last_customer_message_at = ts
        if conv.status in ("resolved", "auto_replied"):
            conv.status = "open"
        conv.updated_at = now

        msg = Message(workspace_id=acct.workspace_id, conversation_id=conv.id, account_id=acct.id,
                      direction="inbound", author="customer", external_id=str(event.external_message_id),
                      text=text, delivery_status="received", created_at=now)
        db.add(msg)
        acct.last_event_at = now
        db.flush()
        enqueue(db, "generate_reply", {"message_id": msg.id}, workspace_id=acct.workspace_id)
        db.commit()
        return msg
    except IntegrityError:
        db.rollback()  # concurrent duplicate delivery
        log.info("duplicate event dropped platform=%s", event.platform)
        return None
