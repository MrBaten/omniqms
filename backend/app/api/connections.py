"""Platforms, connected accounts and the OAuth connect flow."""
import logging
from datetime import timedelta
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.adapters.base import AdapterError, AccountContext
from app.core.config import get_settings
from app.core.crypto import decrypt, encrypt, random_token
from app.core.db import get_db
from app.core.security import ADMIN_ROLES, MemberContext, user_from_request, workspace_member
from app.models import AuditLog, ConnectedAccount, Membership, OAuthState
from app.services import registry
from app.services.util import as_utc, now_utc

log = logging.getLogger("omniqms.connections")
router = APIRouter(tags=["connections"])

STATE_TTL = timedelta(minutes=10)
_ERROR_CODES = {"access_denied", "invalid_state", "expired_state", "not_authenticated", "account_in_use",
                "exchange_failed", "unknown_platform", "no_accounts"}


def redirect_uri(platform: str) -> str:
    return f"{get_settings().public_base_url.rstrip('/')}/oauth/callback/{platform}"


def account_out(a: ConnectedAccount) -> dict:
    return {"id": a.id, "platform": a.platform, "external_id": a.external_id, "name": a.name, "status": a.status,
            "last_event_at": as_utc(a.last_event_at), "created_at": as_utc(a.created_at),
            "last_error": (a.meta or {}).get("last_error")}


@router.get("/api/w/{wid}/platforms")
def platforms(ctx: MemberContext = Depends(workspace_member)):
    out = []
    for a in registry.all_adapters():
        out.append({"platform": a.platform, "label": a.label, "configured": bool(a.is_configured()),
                    "supports_dm": a.supports_dm, "supports_comments": a.supports_comments})
    return out


@router.get("/api/w/{wid}/connections")
def list_connections(ctx: MemberContext = Depends(workspace_member), db: Session = Depends(get_db)):
    rows = db.scalars(select(ConnectedAccount).where(ConnectedAccount.workspace_id == ctx.workspace.id,
                                                     ConnectedAccount.status != "disconnected")
                      .order_by(ConnectedAccount.id))
    return [account_out(a) for a in rows]


@router.post("/api/w/{wid}/connections/{platform}/start")
def start_connect(platform: str, ctx: MemberContext = Depends(workspace_member), db: Session = Depends(get_db)):
    ctx.require_role(*ADMIN_ROLES)
    adapter = registry.find_adapter(platform)
    if adapter is None:
        raise HTTPException(status_code=404, detail="Unknown platform")
    if not adapter.is_configured():
        raise HTTPException(status_code=400, detail="platform_not_configured")
    db.execute(delete(OAuthState).where(OAuthState.created_at < now_utc() - timedelta(days=1)))
    state = random_token(32)
    db.add(OAuthState(state=state, workspace_id=ctx.workspace.id, user_id=ctx.user.id, platform=platform))
    db.commit()
    return {"authorize_url": adapter.authorize_url(state, redirect_uri(platform))}


def _back(**params) -> RedirectResponse:
    return RedirectResponse(f"/app/connections?{urlencode(params)}", status_code=302)


@router.get("/oauth/callback/{platform}")
async def oauth_callback(platform: str, request: Request, db: Session = Depends(get_db)):
    qp = request.query_params
    state_val = (qp.get("state") or "")[:128]
    code = (qp.get("code") or "")[:4096]
    adapter = registry.find_adapter(platform)
    if adapter is None:
        return _back(error="unknown_platform")
    user = user_from_request(request, db)
    if user is None:
        return _back(error="not_authenticated")
    st = db.get(OAuthState, state_val) if state_val else None
    if st is None or st.used or st.platform != platform or st.user_id != user.id:
        return _back(error="invalid_state")
    st.used = True  # single use, even if the rest fails
    db.commit()
    if now_utc() - as_utc(st.created_at) > STATE_TTL:
        return _back(error="expired_state")
    member = db.scalar(select(Membership).where(Membership.user_id == user.id,
                                                Membership.workspace_id == st.workspace_id))
    if member is None or member.role not in ADMIN_ROLES:
        return _back(error="invalid_state")
    if qp.get("error") or not code:
        return _back(error="access_denied")

    try:
        grants = await adapter.exchange_code(code, redirect_uri(platform))
    except Exception as e:  # AdapterError or HTTP errors — never leak details
        log.warning("oauth exchange failed platform=%s err=%s", platform, type(e).__name__)
        return _back(error="exchange_failed")
    if not grants:
        return _back(error="no_accounts")

    connected, in_use = 0, 0
    for g in grants:
        ext = str(g.external_id)
        acct = db.scalar(select(ConnectedAccount).where(ConnectedAccount.platform == platform,
                                                        ConnectedAccount.external_id == ext))
        if acct is not None and acct.workspace_id != st.workspace_id:
            if acct.status != "disconnected":
                in_use += 1
                continue
            # Disconnected elsewhere: release the external id but keep the row, so the previous workspace's
            # conversation history is not wiped by ON DELETE CASCADE when another tenant connects it.
            acct.external_id = f"released-{acct.id}:{ext}"[:128]
            db.flush()
            acct = None
        if acct is None:
            acct = ConnectedAccount(workspace_id=st.workspace_id, platform=platform, external_id=ext)
            db.add(acct)
        acct.name = (g.name or "")[:300]
        acct.access_token_enc = encrypt(g.tokens.access_token)
        acct.refresh_token_enc = encrypt(g.tokens.refresh_token) if g.tokens.refresh_token else acct.refresh_token_enc
        acct.token_expires_at = g.tokens.expires_at
        meta = {**(acct.meta or {}), **(g.meta or {})}
        meta.pop("last_error", None)
        acct.status = "connected"
        try:
            await adapter.on_connected(g)
        except Exception as e:
            acct.status = "needs_reconnect"
            meta["last_error"] = (str(e) if isinstance(e, AdapterError) else "webhook_subscription_failed")[:300]
        acct.meta = meta
        db.add(AuditLog(workspace_id=st.workspace_id, user_id=user.id, action="connect",
                        detail={"platform": platform, "external_id": ext}))
        db.commit()
        connected += 1
    if connected == 0 and in_use:
        return _back(error="account_in_use")
    params = {"connected": platform, "count": connected}
    if in_use:
        params["skipped"] = in_use
    return _back(**params)


@router.delete("/api/w/{wid}/connections/{account_id}")
async def disconnect(account_id: int, ctx: MemberContext = Depends(workspace_member), db: Session = Depends(get_db)):
    ctx.require_role(*ADMIN_ROLES)
    acct = db.scalar(select(ConnectedAccount).where(ConnectedAccount.id == account_id,
                                                    ConnectedAccount.workspace_id == ctx.workspace.id))
    if acct is None or acct.status == "disconnected":
        raise HTTPException(status_code=404, detail="Connection not found")
    adapter = registry.find_adapter(acct.platform)
    if adapter is not None:
        try:
            await adapter.on_disconnected(AccountContext(external_id=acct.external_id,
                                                         access_token=decrypt(acct.access_token_enc) or "",
                                                         meta=dict(acct.meta or {})))
        except Exception as e:
            log.info("on_disconnected failed: %s", type(e).__name__)
    acct.status = "disconnected"
    acct.access_token_enc = None
    acct.refresh_token_enc = None
    acct.token_expires_at = None
    db.add(AuditLog(workspace_id=ctx.workspace.id, user_id=ctx.user.id, action="disconnect",
                    detail={"platform": acct.platform, "external_id": acct.external_id}))
    db.commit()
    return {"ok": True}
