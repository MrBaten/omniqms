"""Knowledge base, settings, stats and sandbox endpoints."""
from datetime import timedelta
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.ai import engine
from app.core.config import get_settings
from app.core.db import get_db
from app.core.security import ADMIN_ROLES, MemberContext, workspace_member
from app.models import ConnectedAccount, Conversation, Message
from app.services import knowledge as kb
from app.services import registry
from app.services.ingest import ingest_event
from app.services.util import as_utc, now_utc, ws_settings

router = APIRouter(prefix="/api/w/{wid}", tags=["workspace"])


# --- knowledge -------------------------------------------------------------------------------
class DocIn(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    content: str = Field(min_length=1, max_length=200_000)
    source_type: Literal["text", "faq"] = "text"


class DocUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=300)
    content: str | None = Field(default=None, min_length=1, max_length=200_000)
    source_type: Literal["text", "faq"] | None = None


class TestIn(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    kind: Literal["dm", "comment"] = "dm"


def doc_out(d) -> dict:
    return {"id": d.id, "title": d.title, "source_type": d.source_type, "content": d.content,
            "created_at": as_utc(d.created_at), "updated_at": as_utc(d.updated_at)}


@router.get("/knowledge")
def list_knowledge(ctx: MemberContext = Depends(workspace_member), db: Session = Depends(get_db)):
    return [doc_out(d) for d in kb.list_docs(db, ctx.workspace.id)]


@router.post("/knowledge")
def create_knowledge(body: DocIn, ctx: MemberContext = Depends(workspace_member), db: Session = Depends(get_db)):
    ctx.require_role(*ADMIN_ROLES)
    if db.scalar(select(func.count()).select_from(kb.KnowledgeDoc).where(kb.KnowledgeDoc.workspace_id == ctx.workspace.id)) >= 1000:
        raise HTTPException(status_code=400, detail="Knowledge base document limit reached")
    return doc_out(kb.create_doc(db, ctx.workspace.id, body.title.strip(), body.content, body.source_type))


@router.put("/knowledge/{doc_id}")
def update_knowledge(doc_id: int, body: DocUpdate, ctx: MemberContext = Depends(workspace_member),
                     db: Session = Depends(get_db)):
    ctx.require_role(*ADMIN_ROLES)
    doc = kb.get_doc(db, ctx.workspace.id, doc_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="Document not found")
    return doc_out(kb.update_doc(db, doc, title=body.title, content=body.content, source_type=body.source_type))


@router.delete("/knowledge/{doc_id}")
def delete_knowledge(doc_id: int, ctx: MemberContext = Depends(workspace_member), db: Session = Depends(get_db)):
    ctx.require_role(*ADMIN_ROLES)
    doc = kb.get_doc(db, ctx.workspace.id, doc_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="Document not found")
    kb.delete_doc(db, doc)
    return {"ok": True}


@router.post("/knowledge/test")
async def test_knowledge(body: TestIn, ctx: MemberContext = Depends(workspace_member), db: Session = Depends(get_db)):
    r = await engine.answer(db, ctx.workspace, body.question, [], kind=body.kind)
    threshold = float(ws_settings(ctx.workspace).get("confidence_threshold", 0.6))
    return {**r, "would_auto_reply": bool(r["answer"]) and not r["needs_human"] and r["confidence"] >= threshold}


# --- settings --------------------------------------------------------------------------------
class SettingsIn(BaseModel):
    auto_reply_enabled: bool | None = None
    confidence_threshold: float | None = Field(default=None, ge=0.0, le=1.0)
    brand_voice: str | None = Field(default=None, max_length=1000)
    reply_language: str | None = Field(default=None, max_length=40)
    escalation_keywords: list[str] | None = Field(default=None, max_length=100)
    business_hours_note: str | None = Field(default=None, max_length=1000)

    @field_validator("escalation_keywords")
    @classmethod
    def _kw(cls, v):
        if v is None:
            return v
        out = []
        for k in v:
            k = (k or "").strip()[:50]
            if k and k not in out:
                out.append(k)
        return out


@router.get("/settings")
def get_ws_settings(ctx: MemberContext = Depends(workspace_member)):
    return ws_settings(ctx.workspace)


@router.put("/settings")
def put_ws_settings(body: SettingsIn, ctx: MemberContext = Depends(workspace_member), db: Session = Depends(get_db)):
    ctx.require_role(*ADMIN_ROLES)
    current = ws_settings(ctx.workspace)
    current.update(body.model_dump(exclude_none=True))
    ctx.workspace.settings = current  # reassign so the JSON column is marked dirty
    db.commit()
    return current


# --- stats -----------------------------------------------------------------------------------
@router.get("/stats")
def stats(ctx: MemberContext = Depends(workspace_member), db: Session = Depends(get_db)):
    wid = ctx.workspace.id
    by_status = dict(db.execute(select(Conversation.status, func.count()).where(Conversation.workspace_id == wid)
                                .group_by(Conversation.status)).all())
    by_platform = dict(db.execute(select(Conversation.platform, func.count()).where(Conversation.workspace_id == wid)
                                  .group_by(Conversation.platform)).all())
    auto_24h = db.scalar(select(func.count()).select_from(Message).where(
        Message.workspace_id == wid, Message.author == "ai", Message.delivery_status == "sent",
        Message.author_user_id.is_(None), Message.created_at >= now_utc() - timedelta(hours=24)))
    return {"open": by_status.get("open", 0), "pending_human": by_status.get("pending_human", 0),
            "auto_replied": by_status.get("auto_replied", 0), "resolved": by_status.get("resolved", 0),
            "auto_replied_24h": auto_24h or 0, "by_platform": by_platform}


# --- sandbox ---------------------------------------------------------------------------------
class SimulateIn(BaseModel):
    kind: Literal["dm", "comment"] = "dm"
    text: str = Field(min_length=1, max_length=2000)
    author_name: str = Field(default="Test Customer", min_length=1, max_length=100)


@router.post("/sandbox/simulate")
def simulate(body: SimulateIn, ctx: MemberContext = Depends(workspace_member), db: Session = Depends(get_db)):
    if not get_settings().sandbox_enabled:
        raise HTTPException(status_code=404, detail="Not found")
    adapter = registry.find_adapter("sandbox")
    if adapter is None:
        raise HTTPException(status_code=404, detail="Not found")
    acct = db.scalar(select(ConnectedAccount).where(ConnectedAccount.workspace_id == ctx.workspace.id,
                                                    ConnectedAccount.platform == "sandbox",
                                                    ConnectedAccount.status == "connected")
                     .order_by(ConnectedAccount.id).limit(1))
    if acct is None:
        raise HTTPException(status_code=400, detail="Connect the Sandbox platform first")
    event = adapter.make_event(acct.external_id, body.kind, body.text, body.author_name)
    msg = ingest_event(db, event)
    if msg is None:
        raise HTTPException(status_code=400, detail="Event was not ingested")
    return {"message_id": msg.id, "conversation_id": msg.conversation_id}
