"""Platform adapter contract (frozen). Core code only talks to platforms through this interface.

Adapters are pure integration code: they never touch the database. Core passes decrypted tokens in
via AccountContext and persists whatever the adapter returns.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Literal, Mapping

Kind = Literal["dm", "comment"]

DM_AI_WINDOW = timedelta(hours=24)  # standard messaging window (AI or human)
DM_HUMAN_WINDOW = timedelta(days=7)  # HUMAN_AGENT tag window (human only)


@dataclass
class TokenBundle:
    access_token: str
    refresh_token: str | None = None
    expires_at: datetime | None = None


@dataclass
class AccountGrant:
    """One connectable account returned from an OAuth code exchange."""
    external_id: str  # FB page id | IG professional account id | YouTube channel id
    name: str
    tokens: TokenBundle
    meta: dict = field(default_factory=dict)  # non-secret info only


@dataclass
class AccountContext:
    """What core hands to the adapter for an already-connected account."""
    external_id: str
    access_token: str
    refresh_token: str | None = None
    token_expires_at: datetime | None = None
    meta: dict = field(default_factory=dict)


@dataclass
class InboundEvent:
    platform: str
    account_external_id: str  # which connected account received it
    kind: Kind
    external_message_id: str  # unique per account; used for dedupe
    thread_external_id: str  # dm: customer's platform-scoped id; comment: top-level comment id
    author_external_id: str
    author_name: str | None
    text: str
    timestamp: datetime
    parent_external_id: str | None = None  # post / media / video id
    is_echo: bool = False  # True if sent by the account itself (core drops these)
    raw: dict = field(default_factory=dict)


@dataclass
class ReplyTarget:
    kind: Kind
    thread_external_id: str  # dm: recipient id; comment: top-level comment id to reply under
    last_customer_message_at: datetime | None
    sent_by_human: bool
    reply_to_external_message_id: str | None = None
    parent_external_id: str | None = None


@dataclass
class SendResult:
    ok: bool
    external_message_id: str | None = None
    error: str | None = None
    retryable: bool = False
    needs_reconnect: bool = False


@dataclass
class PollResult:
    events: list[InboundEvent]
    meta_updates: dict = field(default_factory=dict)  # merged into ConnectedAccount.meta (e.g. cursors)
    tokens: TokenBundle | None = None  # set if tokens were refreshed during polling
    needs_reconnect: bool = False


class AdapterError(Exception):
    def __init__(self, message: str, *, needs_reconnect: bool = False, retryable: bool = False):
        super().__init__(message)
        self.needs_reconnect = needs_reconnect
        self.retryable = retryable


def dm_window(last_customer_message_at: datetime | None, now: datetime | None = None) -> tuple[bool, bool]:
    """Return (ai_allowed, human_allowed) for a DM reply right now."""
    if last_customer_message_at is None:
        return (False, False)
    now = now or datetime.now(timezone.utc)
    last = last_customer_message_at
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    age = now - last
    return (age <= DM_AI_WINDOW, age <= DM_HUMAN_WINDOW)


class PlatformAdapter(ABC):
    platform: str
    label: str
    supports_dm: bool
    supports_comments: bool
    uses_webhooks: bool  # False → core polls via poll()

    @abstractmethod
    def is_configured(self) -> bool:
        """True when the app credentials needed for OAuth are present in settings."""

    @abstractmethod
    def authorize_url(self, state: str, redirect_uri: str) -> str: ...

    @abstractmethod
    async def exchange_code(self, code: str, redirect_uri: str) -> list[AccountGrant]: ...

    async def on_connected(self, grant: AccountGrant) -> None:
        """Post-connect setup, e.g. subscribe the Page to app webhooks. Raise AdapterError on failure."""
        return None

    async def on_disconnected(self, account: AccountContext) -> None:
        return None

    # --- webhooks -------------------------------------------------------------------------
    def verify_token_matches(self, token: str) -> bool:
        """Meta GET handshake: does hub.verify_token match our configured token?"""
        return False

    def verify_webhook_signature(self, body: bytes, headers: Mapping[str, str]) -> bool:
        return False

    def parse_webhook(self, payload: dict) -> list[InboundEvent]:
        return []

    # --- polling --------------------------------------------------------------------------
    async def poll(self, account: AccountContext, since: datetime | None) -> PollResult:
        return PollResult(events=[])

    # --- sending --------------------------------------------------------------------------
    @abstractmethod
    async def send_reply(self, account: AccountContext, target: ReplyTarget, text: str) -> SendResult: ...

    async def refresh_tokens(self, account: AccountContext) -> TokenBundle | None:
        return None
