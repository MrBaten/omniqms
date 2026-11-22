"""Knowledge base CRUD + chunking."""
from __future__ import annotations

import re

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models import KnowledgeChunk, KnowledgeDoc
from app.services.util import now_utc

CHUNK_SIZE = 800
CHUNK_OVERLAP = 150


def chunk_text(text: str, size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[str]:
    text = re.sub(r"[ \t]+", " ", text or "").strip()
    if not text:
        return []
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks: list[str] = []
    buf = ""
    for p in paragraphs:
        if len(p) > size:
            if buf:
                chunks.append(buf)
                buf = ""
            start = 0
            while start < len(p):
                end = min(start + size, len(p))
                if end < len(p):  # try to break on a sentence/space boundary
                    cut = max(p.rfind(". ", start, end), p.rfind("। ", start, end), p.rfind("? ", start, end))
                    if cut <= start + size // 2:
                        cut = p.rfind(" ", start, end)
                    if cut > start + size // 2:
                        end = cut + 1
                chunks.append(p[start:end].strip())
                if end >= len(p):
                    break
                start = max(end - overlap, start + 1)
        elif len(buf) + len(p) + 2 <= size:
            buf = f"{buf}\n\n{p}" if buf else p
        else:
            if buf:
                chunks.append(buf)
            buf = p
    if buf:
        chunks.append(buf)
    return [c for c in chunks if c]


def _rechunk(db: Session, doc: KnowledgeDoc) -> None:
    db.execute(delete(KnowledgeChunk).where(KnowledgeChunk.doc_id == doc.id))
    body = f"{doc.title}\n\n{doc.content}" if doc.title else doc.content
    for i, c in enumerate(chunk_text(body)):
        db.add(KnowledgeChunk(workspace_id=doc.workspace_id, doc_id=doc.id, ordinal=i, text=c))


def create_doc(db: Session, workspace_id: int, title: str, content: str, source_type: str = "text") -> KnowledgeDoc:
    doc = KnowledgeDoc(workspace_id=workspace_id, title=title, content=content, source_type=source_type)
    db.add(doc)
    db.flush()
    _rechunk(db, doc)
    db.commit()
    return doc


def get_doc(db: Session, workspace_id: int, doc_id: int) -> KnowledgeDoc | None:
    return db.scalar(select(KnowledgeDoc).where(KnowledgeDoc.id == doc_id, KnowledgeDoc.workspace_id == workspace_id))


def list_docs(db: Session, workspace_id: int) -> list[KnowledgeDoc]:
    return list(db.scalars(select(KnowledgeDoc).where(KnowledgeDoc.workspace_id == workspace_id)
                           .order_by(KnowledgeDoc.updated_at.desc(), KnowledgeDoc.id.desc())))


def update_doc(db: Session, doc: KnowledgeDoc, *, title: str | None = None, content: str | None = None,
               source_type: str | None = None) -> KnowledgeDoc:
    if title is not None:
        doc.title = title
    if content is not None:
        doc.content = content
    if source_type is not None:
        doc.source_type = source_type
    doc.updated_at = now_utc()
    db.flush()
    _rechunk(db, doc)
    db.commit()
    return doc


def delete_doc(db: Session, doc: KnowledgeDoc) -> None:
    db.execute(delete(KnowledgeChunk).where(KnowledgeChunk.doc_id == doc.id))
    db.delete(doc)
    db.commit()
