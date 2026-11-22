import json
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import respx

from app.adapters import get_adapter
from app.adapters.base import AccountContext, AccountGrant, AdapterError, ReplyTarget, TokenBundle
from app.core.crypto import hmac_sha256_hex

G = "https://graph.instagram.com/v26.0"
NOW = datetime.now(timezone.utc)


@pytest.fixture
def ig():
    return get_adapter("instagram")


def acct(**kw):
    return AccountContext(external_id="IG17", access_token="IGTOKEN", **kw)


def test_authorize_url(ig):
    u = urlparse(ig.authorize_url("S", "https://qms.example.com/oauth/callback/instagram"))
    assert u.netloc == "www.instagram.com" and u.path == "/oauth/authorize"
    q = parse_qs(u.query)
    assert q["client_id"] == ["igapp"] and q["state"] == ["S"] and q["response_type"] == ["code"]
    assert set(q["scope"][0].split(",")) == {
        "instagram_business_basic", "instagram_business_manage_messages", "instagram_business_manage_comments"}


@respx.mock
async def test_exchange_code_chain(ig):
    short = respx.post("https://api.instagram.com/oauth/access_token").mock(
        return_value=httpx.Response(200, json={"access_token": "SHORT", "user_id": 17, "permissions": []}))
    longr = respx.get("https://graph.instagram.com/access_token").mock(
        return_value=httpx.Response(200, json={"access_token": "LONG", "token_type": "bearer", "expires_in": 5183944}))
    me = respx.get(f"{G}/me").mock(return_value=httpx.Response(200, json={"user_id": "IG17", "username": "shop.bd", "id": "app1"}))
    grants = await ig.exchange_code("CODE", "https://qms.example.com/cb")
    form = parse_qs(short.calls.last.request.content.decode())
    assert form["grant_type"] == ["authorization_code"] and form["code"] == ["CODE"]
    assert form["client_secret"] == ["igsecret"]
    q = parse_qs(urlparse(str(longr.calls.last.request.url)).query)
    assert q["grant_type"] == ["ig_exchange_token"] and q["access_token"] == ["SHORT"]
    assert "access_token=LONG" in str(me.calls.last.request.url)
    (g,) = grants
    assert g.external_id == "IG17" and g.name == "shop.bd" and g.tokens.access_token == "LONG"
    assert g.tokens.expires_at > NOW + timedelta(days=59)


@respx.mock
async def test_exchange_code_wrapped_response(ig):
    respx.post("https://api.instagram.com/oauth/access_token").mock(
        return_value=httpx.Response(200, json={"data": [{"access_token": "SHORT", "user_id": "1"}]}))
    respx.get("https://graph.instagram.com/access_token").mock(
        return_value=httpx.Response(200, json={"access_token": "LONG", "expires_in": 100}))
    respx.get(f"{G}/me").mock(return_value=httpx.Response(200, json={"user_id": "1", "username": "u"}))
    (g,) = await ig.exchange_code("C", "r")
    assert g.tokens.access_token == "LONG"


@respx.mock
async def test_exchange_code_error(ig):
    respx.post("https://api.instagram.com/oauth/access_token").mock(
        return_value=httpx.Response(400, json={"error_type": "OAuthException", "code": 400, "error_message": "bad"}))
    with pytest.raises(AdapterError):
        await ig.exchange_code("C", "r")


@respx.mock
async def test_on_connected(ig):
    route = respx.post(f"{G}/me/subscribed_apps").mock(return_value=httpx.Response(200, json={"success": True}))
    await ig.on_connected(AccountGrant("IG17", "u", TokenBundle("T")))
    q = parse_qs(urlparse(str(route.calls.last.request.url)).query)
    assert q["subscribed_fields"] == ["comments,messages"] and q["access_token"] == ["T"]


@respx.mock
async def test_refresh_tokens(ig):
    route = respx.get("https://graph.instagram.com/refresh_access_token").mock(
        return_value=httpx.Response(200, json={"access_token": "NEW", "expires_in": 5184000}))
    assert await ig.refresh_tokens(acct(token_expires_at=NOW + timedelta(days=30))) is None
    assert not route.called
    tb = await ig.refresh_tokens(acct(token_expires_at=NOW + timedelta(days=2)))
    assert tb.access_token == "NEW"
    q = parse_qs(urlparse(str(route.calls.last.request.url)).query)
    assert q["grant_type"] == ["ig_refresh_token"]


@respx.mock
async def test_refresh_expired_token_needs_reconnect(ig):
    respx.get("https://graph.instagram.com/refresh_access_token").mock(
        return_value=httpx.Response(400, json={"error": {"type": "OAuthException", "code": 190}}))
    with pytest.raises(AdapterError) as ei:
        await ig.refresh_tokens(acct(token_expires_at=NOW - timedelta(days=1)))
    assert ei.value.needs_reconnect


def test_signature_and_verify(ig):
    body = b'{"object":"instagram"}'
    assert ig.verify_webhook_signature(body, {"X-Hub-Signature-256": "sha256=" + hmac_sha256_hex("igsecret", body)})
    assert not ig.verify_webhook_signature(body, {"X-Hub-Signature-256": "sha256=" + hmac_sha256_hex("fbsecret", body)})
    assert not ig.verify_webhook_signature(body, {})
    assert ig.verify_token_matches("igverify") and not ig.verify_token_matches("fbverify")


def test_parse_dm(ig):
    payload = {"object": "instagram", "entry": [{"id": "IG17", "time": 1, "messaging": [
        {"sender": {"id": "IGSID1"}, "recipient": {"id": "IG17"}, "timestamp": 1732280000000,
         "message": {"mid": "aWdf1", "text": "Is this available in red?"}},
        {"sender": {"id": "IG17"}, "recipient": {"id": "IGSID1"}, "timestamp": 1732280001000,
         "message": {"mid": "aWdf2", "text": "Yes", "is_echo": True}},
        {"sender": {"id": "IGSID1"}, "recipient": {"id": "IG17"}, "message": {"mid": "aWdf3", "is_deleted": True}},
        {"sender": {"id": "IGSID1"}, "recipient": {"id": "IG17"}, "read": {"mid": "aWdf2"}},
    ]}]}
    evs = ig.parse_webhook(payload)
    assert len(evs) == 2
    assert (evs[0].kind, evs[0].thread_external_id, evs[0].is_echo) == ("dm", "IGSID1", False)
    assert evs[1].is_echo and evs[1].thread_external_id == "IGSID1"


def test_parse_comments(ig):
    payload = {"object": "instagram", "entry": [{"id": "IG17", "time": 1732280000, "changes": [
        {"field": "comments", "value": {"id": "C1", "text": "Price?", "from": {"id": "U1", "username": "rahim"},
                                        "media": {"id": "M1", "media_product_type": "FEED"}}},
        {"field": "comments", "value": {"id": "C2", "text": "Reply", "parent_id": "C1",
                                        "from": {"id": "U2", "username": "karim"}, "media": {"id": "M1"}}},
        {"field": "comments", "value": {"id": "C3", "text": "Thanks!", "from": {"id": "IG17", "username": "shop"},
                                        "media": {"id": "M1"}}},
        {"field": "mentions", "value": {"media_id": "M9"}},
        {"field": "comments", "value": {"id": "C4"}},
    ]}]}
    evs = ig.parse_webhook(payload)
    assert len(evs) == 3
    assert (evs[0].kind, evs[0].thread_external_id, evs[0].parent_external_id, evs[0].author_name) == \
        ("comment", "C1", "M1", "rahim")
    assert evs[1].thread_external_id == "C1"
    assert evs[2].is_echo


@pytest.mark.parametrize("payload", [None, {}, {"object": "page", "entry": [{"id": "x"}]},
                                     {"object": "instagram", "entry": [None, {"changes": [{"field": "comments", "value": {"id": "1", "text": "t"}}]}]}])
def test_parse_malformed(ig, payload):
    assert ig.parse_webhook(payload) == []


def dm(age, human):
    return ReplyTarget(kind="dm", thread_external_id="IGSID1", last_customer_message_at=NOW - age, sent_by_human=human)


@respx.mock
async def test_send_dm_rules(ig):
    route = respx.post(f"{G}/me/messages").mock(return_value=httpx.Response(200, json={"message_id": "out1"}))
    r1 = await ig.send_reply(acct(), dm(timedelta(hours=5), False), "hi")
    assert r1.ok and json.loads(route.calls.last.request.content)["messaging_type"] == "RESPONSE"
    r2 = await ig.send_reply(acct(), dm(timedelta(days=3), True), "hi")
    assert r2.ok and json.loads(route.calls.last.request.content)["tag"] == "HUMAN_AGENT"
    n = route.call_count
    r3 = await ig.send_reply(acct(), dm(timedelta(days=3), False), "hi")
    r4 = await ig.send_reply(acct(), dm(timedelta(days=8), True), "hi")
    assert not r3.ok and not r4.ok and r3.error == r4.error == "outside_messaging_window"
    assert route.call_count == n


@respx.mock
async def test_send_comment_reply(ig):
    route = respx.post(f"{G}/C1/replies").mock(return_value=httpx.Response(200, json={"id": "C9"}))
    t = ReplyTarget(kind="comment", thread_external_id="C1", last_customer_message_at=None, sent_by_human=False)
    res = await ig.send_reply(acct(), t, "DM us")
    assert res.ok and res.external_message_id == "C9"
    assert parse_qs(route.calls.last.request.content.decode())["message"] == ["DM us"]


@respx.mock
async def test_send_errors(ig):
    respx.post(f"{G}/me/messages").mock(return_value=httpx.Response(400, json={"error": {"type": "OAuthException", "code": 190}}))
    res = await ig.send_reply(acct(), dm(timedelta(hours=1), False), "hi")
    assert res.needs_reconnect and "IGTOKEN" not in res.error
    respx.post(f"{G}/me/messages").mock(return_value=httpx.Response(429, json={}))
    res = await ig.send_reply(acct(), dm(timedelta(hours=1), False), "hi")
    assert res.retryable and not res.needs_reconnect
