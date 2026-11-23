"""Platform webhook endpoints (Meta verify handshake + signed event delivery)."""
import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import PlainTextResponse
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.services import registry
from app.services.ingest import ingest_event

log = logging.getLogger("omniqms.webhooks")
router = APIRouter(prefix="/webhooks", tags=["webhooks"])

MAX_BODY = 1_000_000


@router.get("/{platform}")
def verify(platform: str, request: Request):
    adapter = registry.find_adapter(platform)
    if adapter is None:
        raise HTTPException(status_code=404, detail="Not found")
    qp = request.query_params
    mode, token, challenge = qp.get("hub.mode"), qp.get("hub.verify_token") or "", qp.get("hub.challenge") or ""
    if mode == "subscribe" and token and adapter.verify_token_matches(token) and challenge and len(challenge) <= 256:
        log.info("webhook verify ok platform=%s", platform)
        return PlainTextResponse(challenge)
    log.warning("webhook verify failed platform=%s mode=%s", platform, mode)
    raise HTTPException(status_code=403, detail="Verification failed")


async def _read_limited(request: Request) -> bytes:
    cl = request.headers.get("content-length")
    if cl and cl.isdigit() and int(cl) > MAX_BODY:
        raise HTTPException(status_code=413, detail="Payload too large")
    buf = bytearray()
    async for chunk in request.stream():
        buf.extend(chunk)
        if len(buf) > MAX_BODY:
            raise HTTPException(status_code=413, detail="Payload too large")
    return bytes(buf)


@router.post("/{platform}")
async def receive(platform: str, request: Request, db: Session = Depends(get_db)):
    adapter = registry.find_adapter(platform)
    if adapter is None or not adapter.uses_webhooks:
        raise HTTPException(status_code=404, detail="Not found")
    body = await _read_limited(request)
    try:
        valid = adapter.verify_webhook_signature(body, request.headers)
    except Exception:
        valid = False
    if not valid:
        log.warning("webhook signature invalid platform=%s bytes=%d", platform, len(body))
        raise HTTPException(status_code=403, detail="Invalid signature")
    try:
        payload = json.loads(body or b"{}")
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise HTTPException(status_code=400, detail="Invalid JSON")
    try:
        events = adapter.parse_webhook(payload) if isinstance(payload, dict) else []
    except Exception:
        log.exception("webhook parse failed platform=%s", platform)
        events = []
    stored = 0
    for ev in events[:500]:
        try:
            if ingest_event(db, ev) is not None:
                stored += 1
        except Exception:
            db.rollback()
            log.exception("ingest failed platform=%s", platform)
    log.info("webhook platform=%s object=%s events=%d stored=%d", platform,
             str(payload.get("object", ""))[:20] if isinstance(payload, dict) else "", len(events), stored)
    return {"received": len(events), "stored": stored}
