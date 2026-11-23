"""Facebook Pages (comments + Messenger) via Facebook Login for Business."""
from __future__ import annotations

from typing import Mapping
from urllib.parse import urlencode

import httpx

from app.adapters import _common as c
from app.adapters.base import (AccountContext, AccountGrant, AdapterError, InboundEvent,
                               PlatformAdapter, ReplyTarget, SendResult, TokenBundle)
from app.core.config import get_settings

SCOPES = [
    "pages_show_list", "pages_messaging", "pages_manage_metadata", "pages_read_engagement",
    "pages_manage_engagement", "pages_read_user_content", "business_management",
]
MAX_PAGES_FOLLOW = 20


class FacebookAdapter(PlatformAdapter):
    platform = "facebook"
    label = "Facebook Page"
    supports_dm = True
    supports_comments = True
    uses_webhooks = True

    # --- helpers -------------------------------------------------------------------------
    @staticmethod
    def _graph() -> str:
        return f"https://graph.facebook.com/{get_settings().meta_graph_version}"

    def is_configured(self) -> bool:
        s = get_settings()
        return bool(s.meta_app_id and s.meta_app_secret)

    # --- OAuth ---------------------------------------------------------------------------
    def authorize_url(self, state: str, redirect_uri: str) -> str:
        s = get_settings()
        params = {"client_id": s.meta_app_id, "redirect_uri": redirect_uri, "state": state, "response_type": "code"}
        if s.meta_login_config_id:
            # Facebook Login for Business: the saved configuration carries the permissions (no scope list)
            params.update(config_id=s.meta_login_config_id, override_default_response_type="true")
        else:
            params["scope"] = ",".join(SCOPES)
        q = urlencode(params)
        return f"https://www.facebook.com/{s.meta_graph_version}/dialog/oauth?{q}"

    async def exchange_code(self, code: str, redirect_uri: str) -> list[AccountGrant]:
        s = get_settings()
        g = self._graph()
        try:
            async with c.client() as http:
                r = await http.get(f"{g}/oauth/access_token", params={
                    "client_id": s.meta_app_id, "client_secret": s.meta_app_secret,
                    "redirect_uri": redirect_uri, "code": code,
                })
                short = c.raise_for_graph(r, "code exchange").get("access_token")
                if not short:
                    raise AdapterError("code exchange: no access_token")
                r = await http.get(f"{g}/oauth/access_token", params={
                    "grant_type": "fb_exchange_token", "client_id": s.meta_app_id,
                    "client_secret": s.meta_app_secret, "fb_exchange_token": short,
                })
                long_token = c.raise_for_graph(r, "long-lived exchange").get("access_token") or short

                grants: list[AccountGrant] = []
                url: str | None = f"{g}/me/accounts"
                params: dict | None = {"fields": "id,name,access_token,tasks", "limit": 100,
                                       "access_token": long_token}
                for _ in range(MAX_PAGES_FOLLOW):
                    if not url:
                        break
                    r = await http.get(url, params=params)
                    data = c.raise_for_graph(r, "list pages")
                    for p in data.get("data") or []:
                        if not isinstance(p, dict) or not p.get("id") or not p.get("access_token"):
                            continue
                        grants.append(AccountGrant(
                            external_id=str(p["id"]),
                            name=c.truncate(p.get("name"), 300),
                            tokens=TokenBundle(access_token=p["access_token"]),
                            meta={"tasks": p.get("tasks") or []},
                        ))
                    url = (data.get("paging") or {}).get("next")
                    params = None  # 'next' URL already carries its query string
        except httpx.HTTPError as e:
            raise AdapterError(f"network error: {type(e).__name__}", retryable=True) from e
        if not grants:
            raise AdapterError("no Facebook Pages were granted")
        return grants

    async def ensure_app_subscription(self, http: httpx.AsyncClient) -> None:
        """App-level webhook subscription for the Page object (fields messages + feed). Idempotent; Meta
        re-verifies the callback URL. Best effort: a failure here must not block connecting a Page."""
        s = get_settings()
        if not (s.meta_app_id and s.meta_app_secret and s.meta_webhook_verify_token):
            return
        try:
            r = await http.post(f"{self._graph()}/{s.meta_app_id}/subscriptions", data={
                "object": "page",
                "callback_url": f"{s.public_base_url.rstrip('/')}/webhooks/facebook",
                "verify_token": s.meta_webhook_verify_token,
                "fields": "messages,messaging_postbacks,message_echoes,feed",
                "include_values": "true",
                "access_token": f"{s.meta_app_id}|{s.meta_app_secret}",
            })
            c.raise_for_graph(r, "app subscription")
        except Exception as e:  # noqa: BLE001 — best effort, never blocks the Page connection
            import logging
            logging.getLogger("omniqms.facebook").warning("app webhook subscription failed: %s", type(e).__name__)

    async def on_connected(self, grant: AccountGrant) -> None:
        try:
            async with c.client() as http:
                await self.ensure_app_subscription(http)
                r = await http.post(f"{self._graph()}/{grant.external_id}/subscribed_apps", data={
                    "subscribed_fields": "messages,feed",
                    "access_token": grant.tokens.access_token,
                })
                data = c.raise_for_graph(r, "subscribe page")
        except httpx.HTTPError as e:
            raise AdapterError(f"network error: {type(e).__name__}", retryable=True) from e
        if data.get("success") is False:
            raise AdapterError("subscribe page: success=false")

    async def on_disconnected(self, account: AccountContext) -> None:
        try:
            async with c.client() as http:
                await http.delete(f"{self._graph()}/{account.external_id}/subscribed_apps",
                                  params={"access_token": account.access_token})
        except httpx.HTTPError:
            pass  # best effort

    # --- webhooks ------------------------------------------------------------------------
    def verify_token_matches(self, token: str) -> bool:
        return c.verify_token_eq(get_settings().meta_webhook_verify_token, token)

    def verify_webhook_signature(self, body: bytes, headers: Mapping[str, str]) -> bool:
        return c.verify_hub_signature(get_settings().meta_app_secret, body, headers)

    def parse_webhook(self, payload: dict) -> list[InboundEvent]:
        if not isinstance(payload, dict) or payload.get("object") != "page":
            return []
        events: list[InboundEvent] = []
        for entry in payload.get("entry") or []:
            if not isinstance(entry, dict):
                continue
            page_id = str(entry.get("id") or "")
            for m in entry.get("messaging") or []:
                ev = self._parse_messaging(page_id, m)
                if ev:
                    events.append(ev)
            for ch in entry.get("changes") or []:
                ev = self._parse_change(page_id, ch)
                if ev:
                    events.append(ev)
        return events

    def _parse_messaging(self, page_id: str, m) -> InboundEvent | None:
        if not isinstance(m, dict):
            return None
        msg = m.get("message")
        if not isinstance(msg, dict):
            return None  # delivery/read/postback etc.
        text = msg.get("text")
        mid = msg.get("mid")
        if not text or not mid:
            return None
        sender = str((m.get("sender") or {}).get("id") or "")
        recipient = str((m.get("recipient") or {}).get("id") or "")
        is_echo = bool(msg.get("is_echo")) or (bool(page_id) and sender == page_id)
        account = page_id or (sender if is_echo else recipient)
        thread = recipient if is_echo else sender
        if not account or not thread:
            return None
        return InboundEvent(
            platform=self.platform, account_external_id=account, kind="dm",
            external_message_id=str(mid), thread_external_id=thread,
            author_external_id=sender, author_name=None, text=c.truncate(text),
            timestamp=c.parse_ts(m.get("timestamp")), is_echo=is_echo, raw=m,
        )

    def _parse_change(self, page_id: str, ch) -> InboundEvent | None:
        if not isinstance(ch, dict) or ch.get("field") != "feed":
            return None
        v = ch.get("value")
        if not isinstance(v, dict) or v.get("item") != "comment" or v.get("verb") != "add":
            return None
        cid = v.get("comment_id")
        text = v.get("message")
        if not cid or not text or not page_id:
            return None
        frm = v.get("from") or {}
        author = str(frm.get("id") or "")
        post_id = v.get("post_id")
        parent_id = v.get("parent_id")
        thread = str(parent_id) if parent_id and parent_id != post_id else str(cid)
        return InboundEvent(
            platform=self.platform, account_external_id=page_id, kind="comment",
            external_message_id=str(cid), thread_external_id=thread,
            author_external_id=author, author_name=c.truncate(frm.get("name"), 300) or None,
            text=c.truncate(text), timestamp=c.parse_ts(v.get("created_time")),
            parent_external_id=str(post_id) if post_id else None,
            is_echo=author == page_id, raw=ch,
        )

    # --- sending -------------------------------------------------------------------------
    async def send_reply(self, account: AccountContext, target: ReplyTarget, text: str) -> SendResult:
        text = c.truncate(text, 2000 if target.kind == "dm" else 8000)
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
                # Reply under the top-level comment (Facebook nests one level deep).
                reply_to = target.thread_external_id or target.reply_to_external_message_id
                r = await http.post(f"{self._graph()}/{reply_to}/comments",
                                    data={"message": text, "access_token": account.access_token})
                return c.graph_send_result(r)
        except httpx.HTTPError as e:
            return c.network_failure(e)
