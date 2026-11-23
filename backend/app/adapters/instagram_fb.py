"""Instagram API with Facebook Login (Page-linked) — for newer Meta apps where "Instagram Login" is unavailable.

Host graph.facebook.com. The Instagram professional account must be linked to a Facebook Page the user manages.
Login uses Facebook Login for Business with META_IG_LOGIN_CONFIG_ID (instagram_basic, instagram_manage_messages,
instagram_manage_comments, pages_show_list, pages_read_engagement, pages_manage_metadata).
The connected account's external_id is the Instagram account ID; the stored token is the linked Page's token
(required by the Instagram Messaging API) and meta["page_id"] is the linked Page.
"""
from __future__ import annotations

import logging
from typing import Mapping
from urllib.parse import urlencode

import httpx

from app.adapters import _common as c
from app.adapters.base import AccountContext, AccountGrant, AdapterError, ReplyTarget, SendResult, TokenBundle
from app.adapters.instagram import InstagramAdapter
from app.core.config import get_settings

log = logging.getLogger("omniqms.instagram_fb")


class InstagramFacebookLoginAdapter(InstagramAdapter):
    platform = "instagram"
    label = "Instagram"
    supports_dm = True
    supports_comments = True
    uses_webhooks = True

    @staticmethod
    def _graph() -> str:  # type: ignore[override]
        return f"https://graph.facebook.com/{get_settings().meta_graph_version}"

    def is_configured(self) -> bool:
        s = get_settings()
        return bool(s.meta_app_id and s.meta_app_secret and s.meta_ig_login_config_id)

    def authorize_url(self, state: str, redirect_uri: str) -> str:
        s = get_settings()
        q = urlencode({"client_id": s.meta_app_id, "redirect_uri": redirect_uri, "state": state,
                       "response_type": "code", "config_id": s.meta_ig_login_config_id,
                       "override_default_response_type": "true"})
        return f"https://www.facebook.com/{s.meta_graph_version}/dialog/oauth?{q}"

    async def exchange_code(self, code: str, redirect_uri: str) -> list[AccountGrant]:
        s = get_settings()
        g = self._graph()
        try:
            async with c.client() as http:
                r = await http.get(f"{g}/oauth/access_token", params={
                    "client_id": s.meta_app_id, "client_secret": s.meta_app_secret,
                    "redirect_uri": redirect_uri, "code": code})
                token = c.raise_for_graph(r, "code exchange").get("access_token")
                if not token:
                    raise AdapterError("code exchange: no access_token")
                r = await http.get(f"{g}/oauth/access_token", params={
                    "grant_type": "fb_exchange_token", "client_id": s.meta_app_id,
                    "client_secret": s.meta_app_secret, "fb_exchange_token": token})
                try:
                    token = c.raise_for_graph(r, "long-lived exchange").get("access_token") or token
                except AdapterError:
                    pass  # system-user tokens cannot be exchanged and do not expire

                # Candidate Pages: /me/accounts plus Pages / IG accounts listed in the token's granular scopes.
                page_ids: list[str] = []
                r = await http.get(f"{g}/me/accounts", params={"fields": "id", "limit": 100, "access_token": token})
                try:
                    page_ids += [str(p["id"]) for p in c.raise_for_graph(r, "list pages").get("data") or []
                                 if isinstance(p, dict) and p.get("id")]
                except AdapterError:
                    pass
                ig_ids: list[str] = []
                r = await http.get(f"{g}/debug_token", params={
                    "input_token": token, "access_token": f"{s.meta_app_id}|{s.meta_app_secret}"})
                try:
                    for gs in (c.raise_for_graph(r, "debug token").get("data") or {}).get("granular_scopes") or []:
                        scope = str(gs.get("scope", ""))
                        for t in gs.get("target_ids") or []:
                            (page_ids if scope.startswith("pages_") else ig_ids if scope.startswith("instagram_")
                             else []).append(str(t))
                except AdapterError:
                    pass

                grants: list[AccountGrant] = []
                seen: set[str] = set()
                for pid in dict.fromkeys(page_ids):
                    r = await http.get(f"{g}/{pid}", params={
                        "fields": "id,name,access_token,instagram_business_account{id,username,name}",
                        "access_token": token})
                    try:
                        p = c.raise_for_graph(r, "page")
                    except AdapterError:
                        continue
                    ig = p.get("instagram_business_account") or {}
                    if not ig.get("id") or not p.get("access_token") or str(ig["id"]) in seen:
                        continue
                    if ig_ids and str(ig["id"]) not in ig_ids:
                        continue  # the user did not grant this Instagram account
                    seen.add(str(ig["id"]))
                    grants.append(AccountGrant(
                        external_id=str(ig["id"]),
                        name=c.truncate(ig.get("username") or ig.get("name") or p.get("name"), 300),
                        tokens=TokenBundle(access_token=p["access_token"]),
                        meta={"page_id": str(p["id"]), "page_name": c.truncate(p.get("name"), 300),
                              "login": "facebook"}))
                log.info("instagram accounts granted: pages=%d ig_scoped=%d usable=%d",
                         len(set(page_ids)), len(set(ig_ids)), len(grants))
        except httpx.HTTPError as e:
            raise AdapterError(f"network error: {type(e).__name__}", retryable=True) from e
        if not grants:
            raise AdapterError("no Instagram professional account linked to a granted Facebook Page")
        return grants

    async def on_connected(self, grant: AccountGrant) -> None:
        s = get_settings()
        page_id = grant.meta.get("page_id")
        try:
            async with c.client() as http:
                if s.ig_webhook_verify_token:
                    try:  # app-level subscription for the instagram object (best effort)
                        r = await http.post(f"{self._graph()}/{s.meta_app_id}/subscriptions", data={
                            "object": "instagram",
                            "callback_url": f"{s.public_base_url.rstrip('/')}/webhooks/instagram",
                            "verify_token": s.ig_webhook_verify_token,
                            "fields": "messages,messaging_postbacks,comments",
                            "include_values": "true",
                            "access_token": f"{s.meta_app_id}|{s.meta_app_secret}"})
                        c.raise_for_graph(r, "app instagram subscription")
                    except Exception as e:  # noqa: BLE001
                        log.warning("instagram app subscription failed: %s", type(e).__name__)
                if page_id:  # install the app on the linked Page so Instagram events are delivered
                    r = await http.post(f"{self._graph()}/{page_id}/subscribed_apps", data={
                        "subscribed_fields": "messages,feed", "access_token": grant.tokens.access_token})
                    c.raise_for_graph(r, "subscribe linked page")
        except httpx.HTTPError as e:
            raise AdapterError(f"network error: {type(e).__name__}", retryable=True) from e

    async def refresh_tokens(self, account: AccountContext) -> TokenBundle | None:
        return None  # Page tokens from long-lived/system-user tokens do not expire

    # Webhooks for this mode are signed with the Meta app secret.
    def verify_webhook_signature(self, body: bytes, headers: Mapping[str, str]) -> bool:
        return c.verify_hub_signature(get_settings().meta_app_secret, body, headers)

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
                    page_id = account.meta.get("page_id") or "me"
                    r = await http.post(f"{self._graph()}/{page_id}/messages",
                                        json={"recipient": {"id": target.thread_external_id},
                                              "message": {"text": text}, **p},
                                        params={"access_token": account.access_token})
                    return c.graph_send_result(r, id_key="message_id")
                reply_to = target.thread_external_id or target.reply_to_external_message_id
                r = await http.post(f"{self._graph()}/{reply_to}/replies",
                                    data={"message": text, "access_token": account.access_token})
                return c.graph_send_result(r)
        except httpx.HTTPError as e:
            return c.network_failure(e)
