import json
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import respx

from app.adapters import all_adapters, get_adapter
from app.adapters.base import AccountContext, AdapterError, ReplyTarget
from app.adapters.sandbox import SandboxAdapter

API = "https://www.googleapis.com/youtube/v3"
TOKEN = "https://oauth2.googleapis.com/token"
NOW = datetime.now(timezone.utc)


@pytest.fixture
def yt():
    return get_adapter("youtube")


def acct(**kw):
    base = dict(external_id="UCchan", access_token="AT", refresh_token="RT",
                token_expires_at=NOW + timedelta(hours=1))
    base.update(kw)
    return AccountContext(**base)


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def test_flags_and_authorize_url(yt):
    assert yt.uses_webhooks is False and yt.supports_dm is False and yt.supports_comments
    q = parse_qs(urlparse(yt.authorize_url("S", "https://qms.example.com/oauth/callback/youtube")).query)
    assert q["scope"] == ["https://www.googleapis.com/auth/youtube.force-ssl"]
    assert q["access_type"] == ["offline"] and q["prompt"] == ["consent"]
    assert q["include_granted_scopes"] == ["true"] and q["state"] == ["S"] and q["client_id"] == ["gclient"]


@respx.mock
async def test_exchange_code(yt):
    tok = respx.post(TOKEN).mock(return_value=httpx.Response(200, json={
        "access_token": "AT", "refresh_token": "RT", "expires_in": 3599, "token_type": "Bearer"}))
    ch = respx.get(f"{API}/channels").mock(return_value=httpx.Response(200, json={
        "items": [{"id": "UCchan", "snippet": {"title": "My Channel"}}]}))
    (g,) = await yt.exchange_code("CODE", "https://qms.example.com/cb")
    form = parse_qs(tok.calls.last.request.content.decode())
    assert form["grant_type"] == ["authorization_code"] and form["client_secret"] == ["gsecret"]
    assert ch.calls.last.request.headers["Authorization"] == "Bearer AT"
    assert (g.external_id, g.name, g.tokens.refresh_token) == ("UCchan", "My Channel", "RT")


@respx.mock
async def test_exchange_code_requires_refresh_token(yt):
    respx.post(TOKEN).mock(return_value=httpx.Response(200, json={"access_token": "AT", "expires_in": 3599}))
    with pytest.raises(AdapterError, match="refresh_token"):
        await yt.exchange_code("C", "r")


@respx.mock
async def test_exchange_code_no_channel(yt):
    respx.post(TOKEN).mock(return_value=httpx.Response(200, json={"access_token": "AT", "refresh_token": "RT"}))
    respx.get(f"{API}/channels").mock(return_value=httpx.Response(200, json={"items": []}))
    with pytest.raises(AdapterError):
        await yt.exchange_code("C", "r")


@respx.mock
async def test_refresh_tokens(yt):
    route = respx.post(TOKEN).mock(return_value=httpx.Response(200, json={"access_token": "AT2", "expires_in": 3599}))
    assert await yt.refresh_tokens(acct()) is None
    tb = await yt.refresh_tokens(acct(token_expires_at=NOW - timedelta(minutes=1)))
    assert tb.access_token == "AT2" and tb.refresh_token == "RT"
    assert parse_qs(route.calls.last.request.content.decode())["grant_type"] == ["refresh_token"]


@respx.mock
async def test_refresh_invalid_grant(yt):
    respx.post(TOKEN).mock(return_value=httpx.Response(400, json={"error": "invalid_grant"}))
    with pytest.raises(AdapterError) as ei:
        await yt.refresh_tokens(acct(token_expires_at=NOW - timedelta(minutes=1)))
    assert ei.value.needs_reconnect


def thread(tid, ts, author="UCfan", video="V1", replies=(), text="Nice video"):
    return {"id": tid, "snippet": {"videoId": video, "topLevelComment": {"id": tid, "snippet": {
        "textOriginal": text, "authorDisplayName": "Fan", "authorChannelId": {"value": author},
        "publishedAt": iso(ts), "updatedAt": iso(ts), "videoId": video}}},
        "replies": {"comments": [{"id": f"{tid}.{i}", "snippet": {
            "textOriginal": rt, "authorDisplayName": "R", "authorChannelId": {"value": ra},
            "publishedAt": iso(rts), "parentId": tid}} for i, (rt, ra, rts) in enumerate(replies)]}}


@respx.mock
async def test_poll_filters_since_echo_and_paging(yt):
    since = NOW - timedelta(hours=1)
    page1 = {"items": [
        thread("T1", NOW - timedelta(minutes=5), text="How much?"),
        thread("T2", NOW - timedelta(minutes=10), author="UCchan", text="Owner comment"),
        thread("T3", NOW - timedelta(days=2), replies=[("follow up?", "UCfan2", NOW - timedelta(minutes=2))]),
    ], "nextPageToken": "P2"}
    page2 = {"items": [thread("T4", NOW - timedelta(days=3))], "nextPageToken": "P3"}
    route = respx.get(f"{API}/commentThreads").mock(side_effect=[
        httpx.Response(200, json=page1), httpx.Response(200, json=page2),
        httpx.Response(200, json={"items": []})])
    res = await yt.poll(acct(), since)
    ids = [e.external_message_id for e in res.events]
    assert set(ids) == {"T1", "T2", "T3.0"}
    by = {e.external_message_id: e for e in res.events}
    assert by["T2"].is_echo and not by["T1"].is_echo
    assert by["T3.0"].thread_external_id == "T3" and by["T3.0"].parent_external_id == "V1"
    assert route.call_count == 2  # stopped at old thread on page 2
    q = parse_qs(urlparse(str(route.calls[0].request.url)).query)
    assert q["allThreadsRelatedToChannelId"] == ["UCchan"] and q["order"] == ["time"]
    assert "last_seen_at" in res.meta_updates
    assert datetime.fromisoformat(res.meta_updates["last_seen_at"]) > NOW - timedelta(minutes=3)
    assert res.tokens is None


@respx.mock
async def test_poll_max_pages(yt):
    route = respx.get(f"{API}/commentThreads").mock(return_value=httpx.Response(
        200, json={"items": [thread("T", NOW)], "nextPageToken": "again"}))
    await yt.poll(acct(), None)
    assert route.call_count == 3


@respx.mock
async def test_poll_uses_meta_cursor(yt):
    respx.get(f"{API}/commentThreads").mock(return_value=httpx.Response(200, json={
        "items": [thread("T1", NOW - timedelta(minutes=30))]}))
    res = await yt.poll(acct(meta={"last_seen_at": (NOW - timedelta(minutes=10)).isoformat()}), None)
    assert res.events == []


@respx.mock
async def test_poll_refreshes_expired_token(yt):
    respx.post(TOKEN).mock(return_value=httpx.Response(200, json={"access_token": "FRESH", "expires_in": 3599}))
    route = respx.get(f"{API}/commentThreads").mock(return_value=httpx.Response(200, json={"items": []}))
    res = await yt.poll(acct(token_expires_at=NOW - timedelta(minutes=5)), None)
    assert res.tokens.access_token == "FRESH"
    assert route.calls.last.request.headers["Authorization"] == "Bearer FRESH"


@respx.mock
async def test_poll_invalid_grant_needs_reconnect(yt):
    respx.post(TOKEN).mock(return_value=httpx.Response(400, json={"error": "invalid_grant"}))
    res = await yt.poll(acct(token_expires_at=NOW - timedelta(minutes=5)), None)
    assert res.needs_reconnect and res.events == []


@respx.mock
async def test_poll_quota_error(yt):
    respx.get(f"{API}/commentThreads").mock(return_value=httpx.Response(403, json={
        "error": {"code": 403, "errors": [{"reason": "quotaExceeded"}]}}))
    with pytest.raises(AdapterError) as ei:
        await yt.poll(acct(), None)
    assert ei.value.retryable


@respx.mock
async def test_send_reply_and_errors(yt):
    ct = ReplyTarget(kind="comment", thread_external_id="T1", last_customer_message_at=None, sent_by_human=False)
    route = respx.post(f"{API}/comments").mock(return_value=httpx.Response(200, json={"id": "T1.r1"}))
    res = await yt.send_reply(acct(), ct, "Thanks!")
    assert res.ok and res.external_message_id == "T1.r1"
    assert json.loads(route.calls.last.request.content) == {"snippet": {"parentId": "T1", "textOriginal": "Thanks!"}}
    assert "part=snippet" in str(route.calls.last.request.url)

    respx.post(f"{API}/comments").mock(return_value=httpx.Response(403, json={
        "error": {"code": 403, "errors": [{"reason": "quotaExceeded"}]}}))
    res = await yt.send_reply(acct(), ct, "x")
    assert not res.ok and res.retryable and res.error == "youtube_quota_exceeded"

    respx.post(f"{API}/comments").mock(return_value=httpx.Response(401, json={}))
    res = await yt.send_reply(acct(), ct, "x")
    assert res.needs_reconnect

    dm = ReplyTarget(kind="dm", thread_external_id="x", last_customer_message_at=NOW, sent_by_human=True)
    res = await yt.send_reply(acct(), dm, "x")
    assert not res.ok and res.error == "youtube_dm_not_supported"


# --- registry + sandbox -----------------------------------------------------------------

def test_registry(monkeypatch):
    from app.core.config import get_settings
    assert [a.platform for a in all_adapters()] == ["facebook", "instagram", "youtube", "sandbox"]
    with pytest.raises(KeyError):
        get_adapter("tiktok")
    monkeypatch.setenv("SANDBOX_ENABLED", "false")
    get_settings.cache_clear()
    assert "sandbox" not in [a.platform for a in all_adapters()]
    with pytest.raises(KeyError):
        get_adapter("sandbox")


async def test_sandbox_flow():
    sb = get_adapter("sandbox")
    SandboxAdapter.sent.clear()
    url = sb.authorize_url("ST", "ignored")
    q = parse_qs(urlparse(url).query)
    assert url.startswith("https://qms.example.com/oauth/callback/sandbox?")
    assert q == {"code": ["sandbox-ok"], "state": ["ST"]}
    (g,) = await sb.exchange_code("sandbox-ok", "r")
    assert g.external_id.startswith("sandbox-")
    with pytest.raises(AdapterError):
        await sb.exchange_code("forged", "r")
    ev = sb.make_event(g.external_id, "dm", "hello", "Rahim")
    assert ev.kind == "dm" and ev.thread_external_id == ev.author_external_id and ev.platform == "sandbox"
    cev = sb.make_event(g.external_id, "comment", "price?", "Karim")
    assert cev.thread_external_id == cev.external_message_id and cev.parent_external_id
    ctx = AccountContext(external_id=g.external_id, access_token=g.tokens.access_token)
    res = await sb.send_reply(ctx, ReplyTarget("dm", ev.thread_external_id, NOW, False), "Hi Rahim")
    assert res.ok and SandboxAdapter.sent[-1]["text"] == "Hi Rahim"
    old = await sb.send_reply(ctx, ReplyTarget("dm", ev.thread_external_id, NOW - timedelta(days=2), False), "x")
    assert not old.ok
    assert not sb.verify_webhook_signature(b"{}", {"X-Hub-Signature-256": "sha256=x"})
    with pytest.raises(ValueError):
        sb.make_event("a", "email", "x")
