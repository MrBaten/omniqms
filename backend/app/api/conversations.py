from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import exists, func, or_, select
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.security import MemberContext, workspace_member
from app.models import ConnectedAccount, Contact, Conversation, Message
from app.services.replies import ReplyNotAllowed, approve_draft, create_human_reply, reply_window
from app.services.util import as_utc, now_utc

router = APIRouter(prefix="/api/w/{wid}/conversations", tags=["conversations"])


class ReplyIn(BaseModel):
    text: str = Field(min_length=1, max_length=2000)
    resolve: bool = True


class ApproveIn(BaseModel):
    text: str | None = Field(default=None, max_length=2000)


class StatusIn(BaseModel):
    status: Literal["open", "pending_human", "resolved"]


def message_out(m: Message) -> dict:
    return {"id": m.id, "direction": m.direction, "author": m.author, "text": m.text,
            "ai_confidence": m.ai_confidence, "ai_sources": m.ai_sources, "delivery_status": m.delivery_status,
            "error": m.error, "created_at": as_utc(m.created_at)}


def _get_conv(db: Session, wid: int, cid: int) -> Conversation:
    conv = db.scalar(select(Conversation).where(Conversation.id == cid, Conversation.workspace_id == wid))
    if conv is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return conv


def _conv_out(db: Session, c: Conversation) -> dict:
    last = db.scalar(select(Message.text).where(Message.conversation_id == c.id, Message.delivery_status != "draft")
                     .order_by(Message.id.desc()).limit(1))
    return {"id": c.id, "platform": c.platform, "kind": c.kind, "status": c.status,
            "contact": {"display_name": c.contact.display_name if c.contact else ""},
            "account_name": c.account.name if c.account else "",
            "account_status": c.account.status if c.account else None,
            "parent_external_id": c.parent_external_id,
            "last_message_preview": (last or "")[:140],
            "last_customer_message_at": as_utc(c.last_customer_message_at), "updated_at": as_utc(c.updated_at),
            "created_at": as_utc(c.created_at)}


@router.get("")
def list_conversations(ctx: MemberContext = Depends(workspace_member), db: Session = Depends(get_db),
                       status: str | None = Query(default=None, max_length=32),
                       platform: str | None = Query(default=None, max_length=32),
                       kind: str | None = Query(default=None, max_length=16),
                       q: str | None = Query(default=None, max_length=200),
                       limit: int = Query(default=50, ge=1, le=100), offset: int = Query(default=0, ge=0)):
    wid = ctx.workspace.id
    stmt = select(Conversation).where(Conversation.workspace_id == wid)
    if status:
        stmt = stmt.where(Conversation.status == status)
    if platform:
        stmt = stmt.where(Conversation.platform == platform)
    if kind:
        stmt = stmt.where(Conversation.kind == kind)
    if q:
        like = f"%{q.replace('%', '').replace('_', '')}%"
        stmt = stmt.where(or_(
            exists().where(Contact.id == Conversation.contact_id, Contact.display_name.ilike(like)),
            exists().where(Message.conversation_id == Conversation.id, Message.workspace_id == wid,
                           Message.text.ilike(like))))
    total = db.scalar(select(func.count()).select_from(stmt.subquery()))
    rows = db.scalars(stmt.order_by(Conversation.updated_at.desc(), Conversation.id.desc()).limit(limit).offset(offset))
    return {"items": [_conv_out(db, c) for c in rows], "total": total}


@router.get("/{cid}")
def get_conversation(cid: int, ctx: MemberContext = Depends(workspace_member), db: Session = Depends(get_db)):
    conv = _get_conv(db, ctx.workspace.id, cid)
    msgs = db.scalars(select(Message).where(Message.conversation_id == conv.id,
                                            Message.workspace_id == ctx.workspace.id).order_by(Message.id))
    out = _conv_out(db, conv)
    out["messages"] = [message_out(m) for m in msgs]
    out["reply_window"] = reply_window(conv)
    return out


@router.post("/{cid}/reply")
def reply(cid: int, body: ReplyIn, ctx: MemberContext = Depends(workspace_member), db: Session = Depends(get_db)):
    conv = _get_conv(db, ctx.workspace.id, cid)
    try:
        msg = create_human_reply(db, conv, ctx.user, body.text.strip(), resolve=body.resolve)
    except ReplyNotAllowed as e:
        raise HTTPException(status_code=409, detail=e.code)
    return message_out(msg)


@router.post("/{cid}/drafts/{mid}/approve")
def approve(cid: int, mid: int, body: ApproveIn | None = None, ctx: MemberContext = Depends(workspace_member),
            db: Session = Depends(get_db)):
    conv = _get_conv(db, ctx.workspace.id, cid)
    draft = db.scalar(select(Message).where(Message.id == mid, Message.conversation_id == conv.id,
                                            Message.workspace_id == ctx.workspace.id))
    if draft is None:
        raise HTTPException(status_code=404, detail="Draft not found")
    try:
        msg = approve_draft(db, conv, draft, ctx.user, body.text if body else None)
    except ReplyNotAllowed as e:
        raise HTTPException(status_code=409, detail=e.code)
    return message_out(msg)


@router.post("/{cid}/status")
def set_status(cid: int, body: StatusIn, ctx: MemberContext = Depends(workspace_member), db: Session = Depends(get_db)):
    conv = _get_conv(db, ctx.workspace.id, cid)
    conv.status = body.status
    conv.updated_at = now_utc()
    db.commit()
    return {"id": conv.id, "status": conv.status}
