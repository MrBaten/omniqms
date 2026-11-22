"""YouTube channel comments via YouTube Data API v3 (polling; YouTube has no DM API)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import httpx

from app.adapters import _common as c
from app.adapters.base import (AccountContext, AccountGrant, AdapterError, InboundEvent,
                               PlatformAdapter, PollResult, ReplyTarget, SendResult, TokenBundle)
from app.core.config import get_settings

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
API = "https://www.googleapis.com/youtube/v3"
SCOPE = "https://www.googleapis.com/auth/youtube.force-ssl"
MAX_POLL_PAGES = 3
REFRESH_SKEW = timedelta(minutes=2)


def _expiry(expires_in) -> datetime | None:
    try:
        return datetime.now(timezone.utc) + timedelta(seconds=int(expires_in))
    except (TypeError, ValueError):
        return None


def _yt_error_reason(resp: httpx.Response) -> str | None:
    try:
        err = resp.json().get("error") or {}
    except (ValueError, AttributeError):
        return None
    if not isinstance(err, dict):
        return None
    for e in err.get("errors") or []:
        if isinstance(e, dict) and e.get("reason"):
            return str(e["reason"])
    return err.get("status") if isinstance(err.get("status"), str) else None


class YouTubeAdapter(PlatformAdapter):
    platform = "youtube"
    label = "YouTube"
    supports_dm = False
    supports_comments = True
    uses_webhooks = False

    def is_configured(self) -> bool:
        s = get_settings()
        return bool(s.google_client_id and s.google_client_secret)

    # --- OAuth ---------------------------------------------------------------------------
    def authorize_url(self, state: str, redirect_uri: str) -> str:
        q = urlencode({
            "client_id": get_settings().google_client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": SCOPE,
            "access_type": "offline",
            "prompt": "consent",
            "include_granted_scopes": "true",
            "state": state,
        })
        return f"{AUTH_URL}?{q}"

    async def exchange_code(self, code: str, redirect_uri: str) -> list[AccountGrant]:
        s = get_settings()
        try:
            async with c.client() as http:
                r = await http.post(TOKEN_URL, data={
                    "code": code, "client_id": s.google_client_id,
                    "client_secret": s.google_client_secret, "redirect_uri": redirect_uri,
                    "grant_type": "authorization_code",
                })
                if r.status_code >= 400:
                    raise AdapterError(f"code exchange: http_{r.status_code}")
                tok = r.json()
                access, refresh = tok.get("access_token"), tok.get("refresh_token")
                if not access:
                    raise AdapterError("code exchange: no access_token")
                if not refresh:
                    raise AdapterError("code exchange: no refresh_token (offline access not granted)")
                r = await http.get(f"{API}/channels", params={"part": "snippet", "mine": "true"},
                                   headers={"Authorization": f"Bearer {access}"})
                if r.status_code >= 400:
                    raise AdapterError(f"list channels: http_{r.status_code}")
                items = r.json().get("items") or []
        except httpx.HTTPError as e:
            raise AdapterError(f"network error: {type(e).__name__}", retryable=True) from e
        except ValueError as e:
            raise AdapterError("invalid JSON response") from e
        tokens = TokenBundle(access_token=access, refresh_token=refresh,
                             expires_at=_expiry(tok.get("expires_in")))
        grants = [
            AccountGrant(external_id=str(ch["id"]),
                         name=c.truncate((ch.get("snippet") or {}).get("title"), 300),
                         tokens=tokens, meta={})
            for ch in items if isinstance(ch, dict) and ch.get("id")
        ]
        if not grants:
            raise AdapterError("no YouTube channel on this Google account")
        return grants

    async def refresh_tokens(self, account: AccountContext) -> TokenBundle | None:
        exp = account.token_expires_at
        if exp is not None:
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=timezone.utc)
            if exp - datetime.now(timezone.utc) > REFRESH_SKEW:
                return None
        if not account.refresh_token:
            raise AdapterError("no refresh token", needs_reconnect=True)
        s = get_settings()
        try:
            async with c.client() as http:
                r = await http.post(TOKEN_URL, data={
                    "client_id": s.google_client_id, "client_secret": s.google_client_secret,
                    "refresh_token": account.refresh_token, "grant_type": "refresh_token",
                })
        except httpx.HTTPError as e:
            raise AdapterError(f"network error: {type(e).__name__}", retryable=True) from e
        try:
            data = r.json()
        except ValueError:
            data = {}
        if r.status_code >= 400:
            if data.get("error") in ("invalid_grant", "unauthorized_client"):
                raise AdapterError("refresh token: invalid_grant", needs_reconnect=True)
            raise AdapterError(f"refresh token: http_{r.status_code}",
                               retryable=r.status_code >= 500 or r.status_code == 429)
        if not data.get("access_token"):
            raise AdapterError("refresh token: no access_token")
        return TokenBundle(access_token=data["access_token"],
                           refresh_token=data.get("refresh_token") or account.refresh_token,
                           expires_at=_expiry(data.get("expires_in")))

    # --- polling -------------------------------------------------------------------------
    def _event(self, channel: str, top_id: str, cid: str, sn: dict, video: str | None) -> InboundEvent | None:
        text = sn.get("textOriginal") or sn.get("textDisplay")
        if not cid or not text:
            return None
        author = str(((sn.get("authorChannelId") or {}).get("value")) or "")
        return InboundEvent(
            platform=self.platform, account_external_id=channel, kind="comment",
            external_message_id=cid, thread_external_id=top_id,
            author_external_id=author, author_name=c.truncate(sn.get("authorDisplayName"), 300) or None,
            text=c.truncate(text), timestamp=c.parse_ts(sn.get("publishedAt")),
            parent_external_id=video, is_echo=bool(author) and author == channel,
            raw={"id": cid, "snippet": sn},
        )

    async def poll(self, account: AccountContext, since: datetime | None) -> PollResult:
        new_tokens: TokenBundle | None = None
        try:
            new_tokens = await self.refresh_tokens(account)
        except AdapterError as e:
            if e.needs_reconnect:
                return PollResult(events=[], needs_reconnect=True)
            raise
        token = new_tokens.access_token if new_tokens else account.access_token
        if since is not None and since.tzinfo is None:
            since = since.replace(tzinfo=timezone.utc)
        if since is None and account.meta.get("last_seen_at"):
            since = c.parse_ts(account.meta["last_seen_at"])

        events: list[InboundEvent] = []
        newest = since
        page_token: str | None = None
        stop = False
        try:
            async with c.client() as http:
                for _ in range(MAX_POLL_PAGES):
                    params = {"part": "snippet,replies",
                              "allThreadsRelatedToChannelId": account.external_id,
                              "order": "time", "maxResults": 50, "textFormat": "plainText"}
                    if page_token:
                        params["pageToken"] = page_token
                    r = await http.get(f"{API}/commentThreads", params=params,
                                       headers={"Authorization": f"Bearer {token}"})
                    if r.status_code == 401:
                        return PollResult(events=events, tokens=new_tokens, needs_reconnect=True)
                    if r.status_code >= 400:
                        reason = _yt_error_reason(r)
                        raise AdapterError(f"poll: {reason or 'http_' + str(r.status_code)}",
                                           retryable=reason == "quotaExceeded" or r.status_code >= 500)
                    data = r.json()
                    for th in data.get("items") or []:
                        if not isinstance(th, dict):
                            continue
                        sn = th.get("snippet") or {}
                        top = sn.get("topLevelComment") or {}
                        top_id = str(top.get("id") or th.get("id") or "")
                        top_sn = top.get("snippet") or {}
                        video = sn.get("videoId") or top_sn.get("videoId")
                        # Thread order is by latest activity, so check replies even when the
                        # top-level comment is old.
                        thread_activity = False
                        cands = [(top_id, top_sn)] + [
                            (str(rp.get("id") or ""), rp.get("snippet") or {})
                            for rp in ((th.get("replies") or {}).get("comments") or [])
                            if isinstance(rp, dict)
                        ]
                        for cid, csn in cands:
                            ts = c.parse_ts(csn.get("publishedAt"))
                            if since is not None and ts <= since:
                                continue
                            thread_activity = True
                            ev = self._event(account.external_id, top_id, cid, csn, video)
                            if ev:
                                events.append(ev)
                                if newest is None or ts > newest:
                                    newest = ts
                        if since is not None and not thread_activity:
                            upd = c.parse_ts(top_sn.get("updatedAt") or top_sn.get("publishedAt"))
                            if upd <= since:
                                stop = True
                                break
                    page_token = data.get("nextPageToken")
                    if stop or not page_token:
                        break
        except httpx.HTTPError as e:
            raise AdapterError(f"network error: {type(e).__name__}", retryable=True) from e
        meta = {"last_seen_at": newest.isoformat()} if newest else {}
        events.sort(key=lambda e: e.timestamp)
        return PollResult(events=events, meta_updates=meta, tokens=new_tokens)

    # --- sending -------------------------------------------------------------------------
    async def send_reply(self, account: AccountContext, target: ReplyTarget, text: str) -> SendResult:
        if target.kind != "comment":
            return SendResult(ok=False, error="youtube_dm_not_supported")
        text = c.truncate(text, 10000)
        if not text.strip():
            return SendResult(ok=False, error="empty_text")
        new_tokens = None
        try:
            new_tokens = await self.refresh_tokens(account)
        except AdapterError as e:
            return SendResult(ok=False, error=str(e), needs_reconnect=e.needs_reconnect,
                              retryable=e.retryable)
        token = new_tokens.access_token if new_tokens else account.access_token
        if new_tokens:
            # Let core persist refreshed tokens via a later poll; keep using the new one now.
            account.access_token = new_tokens.access_token
            account.token_expires_at = new_tokens.expires_at
        try:
            async with c.client() as http:
                r = await http.post(f"{API}/comments", params={"part": "snippet"},
                                    json={"snippet": {"parentId": target.thread_external_id,
                                                      "textOriginal": text}},
                                    headers={"Authorization": f"Bearer {token}"})
        except httpx.HTTPError as e:
            return c.network_failure(e)
        if r.status_code < 400:
            try:
                cid = r.json().get("id")
            except ValueError:
                cid = None
            return SendResult(ok=True, external_message_id=cid)
        reason = _yt_error_reason(r)
        if r.status_code == 401:
            return SendResult(ok=False, error="youtube_unauthorized", needs_reconnect=True)
        if reason == "quotaExceeded" or r.status_code == 429:
            return SendResult(ok=False, error="youtube_quota_exceeded", retryable=True)
        if r.status_code >= 500:
            return SendResult(ok=False, error=f"youtube_http_{r.status_code}", retryable=True)
        return SendResult(ok=False, error=f"youtube_{reason or 'http_' + str(r.status_code)}")
