"""Shared helpers for platform adapters (internal)."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping

import httpx

from app.adapters.base import (AdapterError, ReplyTarget, SendResult, dm_window)
from app.core.crypto import constant_time_equals, hmac_sha256_hex

HTTP_TIMEOUT = 15.0
MAX_TEXT = 5000

# Graph API error codes
_RECONNECT_CODES = {190, 102, 463, 467}
_RETRY_CODES = {1, 2, 4, 17, 32, 341, 613}


def client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=HTTP_TIMEOUT)


def truncate(text: Any, limit: int = MAX_TEXT) -> str:
    if text is None:
        return ""
    s = str(text)
    return s[:limit]


def parse_ts(value: Any) -> datetime:
    """Parse epoch ms/s or ISO-8601 into an aware UTC datetime; fall back to now."""
    try:
        if isinstance(value, (int, float)):
            v = float(value)
            if v > 1e11:  # milliseconds
                v /= 1000.0
            return datetime.fromtimestamp(v, tz=timezone.utc)
        if isinstance(value, str) and value:
            if value.isdigit():
                return parse_ts(int(value))
            s = value.replace("Z", "+00:00")
            # Meta sometimes uses +0000 (no colon)
            if len(s) >= 5 and (s[-5] in "+-") and s[-4:].isdigit() and ":" not in s[-5:]:
                s = s[:-2] + ":" + s[-2:]
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
    except (ValueError, OverflowError, OSError):
        pass
    return datetime.now(timezone.utc)


def verify_hub_signature(secret: str, body: bytes, headers: Mapping[str, str]) -> bool:
    if not secret:
        return False
    sig = None
    for k, v in headers.items():
        if k.lower() == "x-hub-signature-256":
            sig = v
            break
    if not sig or not isinstance(sig, str) or not sig.startswith("sha256="):
        return False
    expected = hmac_sha256_hex(secret, body)
    return constant_time_equals(expected, sig[len("sha256="):].strip().lower())


def verify_token_eq(configured: str, given: str) -> bool:
    if not configured or not given:
        return False
    return constant_time_equals(configured, given)


def _graph_error(resp: httpx.Response) -> tuple[int | None, str]:
    try:
        data = resp.json()
    except ValueError:
        return None, f"http_{resp.status_code}"
    err = data.get("error") if isinstance(data, dict) else None
    if isinstance(err, dict):
        code = err.get("code")
        etype = err.get("type") or "error"
        try:
            code = int(code) if code is not None else None
        except (TypeError, ValueError):
            code = None
        # Never echo the raw message: it can contain request params. Keep type + code only.
        return code, f"graph_error:{etype}:{code}"
    return None, f"http_{resp.status_code}"


def graph_send_result(resp: httpx.Response, id_key: str = "id") -> SendResult:
    if resp.status_code < 400:
        try:
            data = resp.json()
        except ValueError:
            data = {}
        mid = None
        if isinstance(data, dict):
            mid = data.get(id_key) or data.get("message_id") or data.get("id")
        return SendResult(ok=True, external_message_id=str(mid) if mid else None)
    code, err = _graph_error(resp)
    if resp.status_code == 401 or code in _RECONNECT_CODES:
        return SendResult(ok=False, error=err, needs_reconnect=True)
    if resp.status_code == 429 or resp.status_code >= 500 or code in _RETRY_CODES:
        return SendResult(ok=False, error=err, retryable=True)
    return SendResult(ok=False, error=err)


def raise_for_graph(resp: httpx.Response, what: str) -> dict:
    """Return JSON on success; raise AdapterError (without token data) on failure."""
    if resp.status_code < 400:
        try:
            data = resp.json()
        except ValueError as e:
            raise AdapterError(f"{what}: invalid JSON response") from e
        if isinstance(data, dict) and "error" in data:
            code, err = _graph_error(resp)
            raise AdapterError(f"{what}: {err}", needs_reconnect=code in _RECONNECT_CODES)
        return data if isinstance(data, dict) else {"data": data}
    code, err = _graph_error(resp)
    raise AdapterError(
        f"{what}: {err}",
        needs_reconnect=resp.status_code == 401 or code in _RECONNECT_CODES,
        retryable=resp.status_code == 429 or resp.status_code >= 500 or code in _RETRY_CODES,
    )


def dm_send_params(target: ReplyTarget) -> dict | SendResult:
    """Messaging window rules shared by Messenger and Instagram DMs."""
    ai_ok, human_ok = dm_window(target.last_customer_message_at)
    if ai_ok:
        return {"messaging_type": "RESPONSE"}
    if target.sent_by_human and human_ok:
        return {"messaging_type": "MESSAGE_TAG", "tag": "HUMAN_AGENT"}
    return SendResult(ok=False, error="outside_messaging_window")


def network_failure(exc: Exception) -> SendResult:
    return SendResult(ok=False, error=f"network_error:{type(exc).__name__}", retryable=True)
