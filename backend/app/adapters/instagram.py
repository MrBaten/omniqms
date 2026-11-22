"""Instagram professional accounts via the Instagram API with Instagram Login."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Mapping
from urllib.parse import urlencode

import httpx

from app.adapters import _common as c
from app.adapters.base import (AccountContext, AccountGrant, AdapterError, InboundEvent,
                               PlatformAdapter, ReplyTarget, SendResult, TokenBundle)
from app.core.config import get_settings

SCOPES = ["instagram_business_basic", "instagram_business_manage_messages",
          "instagram_business_manage_comments"]
REFRESH_BEFORE = timedelta(days=7)


def _expiry(expires_in) -> datetime | None:
    try:
        return datetime.now(timezone.utc) + timedelta(seconds=int(expires_in))
    except (TypeError, ValueError):
        return None


class InstagramAdapter(PlatformAdapter):
    platform = "instagram"
    label = "Instagram"
    supports_dm = True
    supports_comments = True
    uses_webhooks = True

    @staticmethod
    def _graph() -> str:
        return f"https://graph.instagram.com/{get_settings().meta_graph_version}"

    def is_configured(self) -> bool:
        s = get_settings()
        return bool(s.ig_app_id and s.ig_app_secret)

    # --- OAuth ---------------------------------------------------------------------------
    def authorize_url(self, state: str, redirect_uri: str) -> str:
        q = urlencode({
            "client_id": get_settings().ig_app_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "state": state,
            "scope": ",".join(SCOPES),
        })
        return f"https://www.instagram.com/oauth/authorize?{q}"

    async def exchange_code(self, code: str, redirect_uri: str) -> list[AccountGrant]:
        s = get_settings()
        try:
            async with c.client() as http:
                r = await http.post("https://api.instagram.com/oauth/access_token", data={
                    "client_id": s.ig_app_id, "client_secret": s.ig_app_secret,
                    "grant_type": "authorization_code", "redirect_uri": redirect_uri,
                    "code": code,
                })
                data = c.raise_for_graph(r, "code exchange")
                # Response may be flat or wrapped in data[0]
                if isinstance(data.get("data"), list) and data["data"]:
                    data = data["data"][0]
                short = data.get("access_token")
                if not short:
                    raise AdapterError("code exchange: no access_token")
                r = await http.get("https://graph.instagram.com/access_token", params={
                    "grant_type": "ig_exchange_token", "client_secret": s.ig_app_secret,
                    "access_token": short,
                })
                ll = c.raise_for_graph(r, "long-lived exchange")
                token = ll.get("access_token") or short
                expires_at = _expiry(ll.get("expires_in"))
                r = await http.get(f"{self._graph()}/me", params={
                    "fields": "user_id,username", "access_token": token,
                })
                me = c.raise_for_graph(r, "fetch profile")
        except httpx.HTTPError as e:
            raise AdapterError(f"network error: {type(e).__name__}", retryable=True) from e
        uid = me.get("user_id") or me.get("id")
        if not uid:
            raise AdapterError("fetch profile: no user_id")
        return [AccountGrant(
            external_id=str(uid), name=c.truncate(me.get("username"), 300),
            tokens=TokenBundle(access_token=token, expires_at=expires_at),
            meta={"username": me.get("username"), "app_scoped_id": me.get("id")},
        )]

    async def on_connected(self, grant: AccountGrant) -> None:
        try:
            async with c.client() as http:
                r = await http.post(f"{self._graph()}/me/subscribed_apps", params={
                    "subscribed_fields": "comments,messages",
                    "access_token": grant.tokens.access_token,
                })
                data = c.raise_for_graph(r, "subscribe account")
        except httpx.HTTPError as e:
            raise AdapterError(f"network error: {type(e).__name__}", retryable=True) from e
        if data.get("success") is False:
            raise AdapterError("subscribe account: success=false")

    async def refresh_tokens(self, account: AccountContext) -> TokenBundle | None:
        exp = account.token_expires_at
        if exp is not None:
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=timezone.utc)
            if exp - datetime.now(timezone.utc) > REFRESH_BEFORE:
                return None
        try:
            async with c.client() as http:
                r = await http.get("https://graph.instagram.com/refresh_access_token", params={
                    "grant_type": "ig_refresh_token", "access_token": account.access_token,
                })
                data = c.raise_for_graph(r, "refresh token")
        except httpx.HTTPError as e:
            raise AdapterError(f"network error: {type(e).__name__}", retryable=True) from e
        tok = data.get("access_token")
        if not tok:
            raise AdapterError("refresh token: no access_token")
        return TokenBundle(access_token=tok, expires_at=_expiry(data.get("expires_in")))

    # --- webhooks ------------------------------------------------------------------------
    def verify_token_matches(self, token: str) -> bool:
        return c.verify_token_eq(get_settings().ig_webhook_verify_token, token)

    def verify_webhook_signature(self, body: bytes, headers: Mapping[str, str]) -> bool:
        return c.verify_hub_signature(get_settings().ig_app_secret, body, headers)

    def parse_webhook(self, payload: dict) -> list[InboundEvent]:
        if not isinstance(payload, dict) or payload.get("object") != "instagram":
            return []
        events: list[InboundEvent] = []
        for entry in payload.get("entry") or []:
            if not isinstance(entry, dict):
                continue
            acct = str(entry.get("id") or "")
            for m in entry.get("messaging") or []:
                ev = self._parse_messaging(acct, m)
                if ev:
                    events.append(ev)
            for ch in entry.get("changes") or []:
                ev = self._parse_change(acct, ch)
                if ev:
                    events.append(ev)
        return events

    def _parse_messaging(self, acct: str, m) -> InboundEvent | None:
        if not isinstance(m, dict):
            return None
        msg = m.get("message")
        if not isinstance(msg, dict) or msg.get("is_deleted"):
            return None
        text, mid = msg.get("text"), msg.get("mid")
        if not text or not mid:
            return None
        sender = str((m.get("sender") or {}).get("id") or "")
        recipient = str((m.get("recipient") or {}).get("id") or "")
        is_echo = bool(msg.get("is_echo")) or (bool(acct) and sender == acct)
        account = acct or (sender if is_echo else recipient)
        thread = recipient if is_echo else sender
        if not account or not thread:
            return None
        return InboundEvent(
            platform=self.platform, account_external_id=account, kind="dm",
            external_message_id=str(mid), thread_external_id=thread,
            author_external_id=sender, author_name=None, text=c.truncate(text),
            timestamp=c.parse_ts(m.get("timestamp")), is_echo=is_echo, raw=m,
        )

    def _parse_change(self, acct: str, ch) -> InboundEvent | None:
        if not isinstance(ch, dict) or ch.get("field") not in ("comments", "live_comments"):
            return None
        v = ch.get("value")
        if not isinstance(v, dict) or not acct:
            return None
        cid, text = v.get("id"), v.get("text")
        if not cid or not text:
            return None
        frm = v.get("from") or {}
        author = str(frm.get("id") or "")
        media_id = (v.get("media") or {}).get("id")
        parent_id = v.get("parent_id")
        return InboundEvent(
            platform=self.platform, account_external_id=acct, kind="comment",
            external_message_id=str(cid),
            thread_external_id=str(parent_id) if parent_id else str(cid),
            author_external_id=author, author_name=c.truncate(frm.get("username"), 300) or None,
            text=c.truncate(text), timestamp=c.parse_ts(v.get("timestamp") or ch.get("time")),
            parent_external_id=str(media_id) if media_id else None,
            is_echo=author == acct, raw=ch,
        )

    # --- sending -------------------------------------------------------------------------
    async def send_reply(self, account: AccountContext, target: ReplyTarget, text: str) -> SendResult:
        text = c.truncate(text, 1000 if target.kind == "dm" else 2200)
        if not text.strip():
            return SendResult(ok=False, error="empty_text")
        try:
            async with c.client() as http:
                if target.kind == "dm":
                    p = c.dm_send_params(target)
                    if isinstance(p, SendResult):
                        return p
                    body = {"recipient": {"id": target.thread_external_id},
                            "message": {"text": text}, **p}
                    r = await http.post(f"{self._graph()}/me/messages", json=body,
                                        params={"access_token": account.access_token})
                    return c.graph_send_result(r, id_key="message_id")
                reply_to = target.thread_external_id or target.reply_to_external_message_id
                r = await http.post(f"{self._graph()}/{reply_to}/replies",
                                    data={"message": text, "access_token": account.access_token})
                return c.graph_send_result(r)
        except httpx.HTTPError as e:
            return c.network_failure(e)
