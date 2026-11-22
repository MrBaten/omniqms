"""Sandbox platform: simulates a social network so the full pipeline runs without real credentials."""
from __future__ import annotations

import secrets
from datetime import datetime, timezone
from typing import Mapping
from urllib.parse import urlencode

from app.adapters import _common as c
from app.adapters.base import (AccountContext, AccountGrant, AdapterError, InboundEvent, Kind,
                               PlatformAdapter, ReplyTarget, SendResult, TokenBundle)
from app.core.config import get_settings


class SandboxAdapter(PlatformAdapter):
    platform = "sandbox"
    label = "Sandbox (test platform)"
    supports_dm = True
    supports_comments = True
    uses_webhooks = False

    # Class-level so tests can inspect it regardless of instance.
    sent: list[dict] = []

    def is_configured(self) -> bool:
        return bool(get_settings().sandbox_enabled)

    def authorize_url(self, state: str, redirect_uri: str) -> str:
        base = get_settings().public_base_url.rstrip("/")
        return f"{base}/oauth/callback/sandbox?{urlencode({'code': 'sandbox-ok', 'state': state})}"

    async def exchange_code(self, code: str, redirect_uri: str) -> list[AccountGrant]:
        if code != "sandbox-ok":
            raise AdapterError("sandbox: invalid code")
        ext = f"sandbox-{secrets.token_hex(6)}"
        return [AccountGrant(external_id=ext, name="Sandbox Page",
                             tokens=TokenBundle(access_token=f"sbx-{secrets.token_urlsafe(16)}"),
                             meta={"sandbox": True})]

    def verify_webhook_signature(self, body: bytes, headers: Mapping[str, str]) -> bool:
        return False  # sandbox events are injected internally, never via public webhooks

    @staticmethod
    def make_event(account_external_id: str, kind: Kind, text: str,
                   author_name: str | None = None, *, author_external_id: str | None = None,
                   thread_external_id: str | None = None) -> InboundEvent:
        if kind not in ("dm", "comment"):
            raise ValueError("kind must be 'dm' or 'comment'")
        name = c.truncate(author_name or "Test Customer", 300)
        author = author_external_id or "sbx-user-" + (
            secrets.token_hex(4) if author_name is None else
            "".join(ch for ch in name.lower() if ch.isalnum())[:40] or secrets.token_hex(4))
        mid = f"sbx-msg-{secrets.token_hex(8)}"
        thread = thread_external_id or (author if kind == "dm" else mid)
        return InboundEvent(
            platform="sandbox", account_external_id=account_external_id, kind=kind,
            external_message_id=mid, thread_external_id=thread, author_external_id=author,
            author_name=name, text=c.truncate(text), timestamp=datetime.now(timezone.utc),
            parent_external_id="sbx-post-1" if kind == "comment" else None,
        )

    async def send_reply(self, account: AccountContext, target: ReplyTarget, text: str) -> SendResult:
        if not (text or "").strip():
            return SendResult(ok=False, error="empty_text")
        if target.kind == "dm":
            p = c.dm_send_params(target)
            if isinstance(p, SendResult):
                return p
        mid = f"sbx-out-{secrets.token_hex(8)}"
        SandboxAdapter.sent.append({
            "account": account.external_id, "kind": target.kind,
            "thread": target.thread_external_id, "text": text, "id": mid,
            "by_human": target.sent_by_human,
        })
        return SendResult(ok=True, external_message_id=mid)
