import json
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import respx

from app.adapters import get_adapter
from app.adapters.facebook import FacebookAdapter
from app.adapters.base import AccountContext, AccountGrant, AdapterError, ReplyTarget, TokenBundle
from app.core.crypto import hmac_sha256_hex

G = "https://graph.facebook.com/v26.0"
NOW = datetime.now(timezone.utc)


@pytest.fixture
def fb():
    return get_adapter("facebook")


def acct():
    return AccountContext(external_id="PAGE1", access_token="PAGETOKEN-secret")


def test_configured_and_authorize_url(fb):
    assert fb.is_configured()
    url = fb.authorize_url("st4te", "https://qms.example.com/oauth/callback/facebook")
    u = urlparse(url)
    assert u.netloc == "www.facebook.com" and u.path == "/v26.0/dialog/oauth"
    q = parse_qs(u.query)
    assert q["client_id"] == ["fbapp"]
    assert q["state"] == ["st4te"]
    assert q["response_type"] == ["code"]
    assert q["redirect_uri"] == ["https://qms.example.com/oauth/callback/facebook"]
    scopes = q["scope"][0].split(",")
    assert "pages_messaging" in scopes and "pages_manage_engagement" in scopes


def test_not_configured(fb, monkeypatch):
    from app.core.config import get_settings
    monkeypatch.setenv("META_APP_SECRET", "")
    get_settings.cache_clear()
    assert not fb.is_configured()


@respx.mock
async def test_exchange_code_chain_with_paging(fb):
    def token_route(request):
        q = parse_qs(urlparse(str(request.url)).query)
        if q.get("grant_type") == ["fb_exchange_token"]:
            assert q["fb_exchange_token"] == ["SHORT"]
            return httpx.Response(200, json={"access_token": "LONG"})
        assert q["code"] == ["CODE"] and q["client_secret"] == ["fbsecret"]
        return httpx.Response(200, json={"access_token": "SHORT"})

    respx.get(f"{G}/oauth/access_token").mock(side_effect=token_route)
    respx.get(f"{G}/me/accounts").mock(return_value=httpx.Response(200, json={
        "data": [{"id": "P1", "name": "Page One", "access_token": "PT1"},
                 {"id": "bad"}],  # malformed (no token) -> skipped
        "paging": {"next": "https://graph.facebook.com/v26.0/me/accounts/page2?after=x"},
    }))
    respx.get(f"{G}/me/accounts/page2").mock(return_value=httpx.Response(200, json={
        "data": [{"id": "P2", "name": "Page Two", "access_token": "PT2"}], "paging": {}}))
    grants = await fb.exchange_code("CODE", "https://qms.example.com/cb")
    assert [(g.external_id, g.name, g.tokens.access_token) for g in grants] == [
        ("P1", "Page One", "PT1"), ("P2", "Page Two", "PT2")]
    # /me/accounts called with the long-lived token
    first_accounts_call = [c for c in respx.calls if c.request.url.path == "/v26.0/me/accounts"][0]
    assert "access_token=LONG" in str(first_accounts_call.request.url)


@respx.mock
async def test_exchange_code_error_has_no_token(fb):
    respx.get(f"{G}/oauth/access_token").mock(return_value=httpx.Response(
        400, json={"error": {"message": "bad code CODE-secret", "type": "OAuthException", "code": 100}}))
    with pytest.raises(AdapterError) as ei:
        await fb.exchange_code("CODE-secret", "https://x/cb")
    assert "CODE-secret" not in str(ei.value)


@respx.mock
async def test_exchange_code_no_pages(fb):
    respx.get(f"{G}/oauth/access_token").mock(return_value=httpx.Response(200, json={"access_token": "T"}))
    respx.get(f"{G}/me/accounts").mock(return_value=httpx.Response(200, json={"data": []}))
    with pytest.raises(AdapterError):
        await fb.exchange_code("C", "https://x/cb")


@respx.mock
async def test_on_connected_subscribes(fb):
    route = respx.post(f"{G}/P1/subscribed_apps").mock(return_value=httpx.Response(200, json={"success": True}))
    await fb.on_connected(AccountGrant("P1", "Page", TokenBundle("PT1")))
    body = parse_qs(route.calls.last.request.content.decode())
    assert body["subscribed_fields"] == ["messages,feed"]
    assert body["access_token"] == ["PT1"]


@respx.mock
async def test_on_connected_failure(fb):
    respx.post(f"{G}/P1/subscribed_apps").mock(return_value=httpx.Response(
        403, json={"error": {"type": "OAuthException", "code": 200}}))
    with pytest.raises(AdapterError):
        await fb.on_connected(AccountGrant("P1", "Page", TokenBundle("PT1")))


def test_signature(fb):
    body = b'{"object":"page"}'
    good = "sha256=" + hmac_sha256_hex("fbsecret", body)
    assert fb.verify_webhook_signature(body, {"X-Hub-Signature-256": good})
    assert fb.verify_webhook_signature(body, {"x-hub-signature-256": good})
    assert not fb.verify_webhook_signature(body + b" ", {"X-Hub-Signature-256": good})
    assert not fb.verify_webhook_signature(body, {"X-Hub-Signature-256": "sha256=deadbeef"})
    assert not fb.verify_webhook_signature(body, {})
    assert not fb.verify_webhook_signature(body, {"X-Hub-Signature-256": hmac_sha256_hex("fbsecret", body)})


def test_signature_secret_missing(fb, monkeypatch):
    from app.core.config import get_settings
    monkeypatch.setenv("META_APP_SECRET", "")
    get_settings.cache_clear()
    body = b"{}"
    assert not fb.verify_webhook_signature(body, {"X-Hub-Signature-256": "sha256=" + hmac_sha256_hex("", body)})


def test_verify_token(fb):
    assert fb.verify_token_matches("fbverify")
    assert not fb.verify_token_matches("nope")
    assert not fb.verify_token_matches("")


def test_parse_dm_and_echo(fb):
    payload = {"object": "page", "entry": [{"id": "PAGE1", "time": 1, "messaging": [
        {"sender": {"id": "USER9"}, "recipient": {"id": "PAGE1"}, "timestamp": 1732280000000,
         "message": {"mid": "m_1", "text": "Do you deliver to Dhaka?"}},
        {"sender": {"id": "PAGE1"}, "recipient": {"id": "USER9"}, "timestamp": 1732280001000,
         "message": {"mid": "m_2", "text": "Yes!", "is_echo": True}},
        {"sender": {"id": "USER9"}, "recipient": {"id": "PAGE1"}, "delivery": {"mids": ["m_2"]}},
        {"sender": {"id": "USER9"}, "recipient": {"id": "PAGE1"}, "read": {"watermark": 1}},
        {"sender": {"id": "USER9"}, "recipient": {"id": "PAGE1"},
         "message": {"mid": "m_3", "attachments": [{"type": "image"}]}},
    ]}]}
    evs = fb.parse_webhook(payload)
    assert len(evs) == 2
    a, b = evs
    assert (a.kind, a.account_external_id, a.thread_external_id, a.author_external_id, a.is_echo) == \
        ("dm", "PAGE1", "USER9", "USER9", False)
    assert a.text == "Do you deliver to Dhaka?" and a.external_message_id == "m_1"
    assert a.timestamp.year == 2024
    assert b.is_echo and b.thread_external_id == "USER9"


def test_parse_comments(fb):
    payload = {"object": "page", "entry": [{"id": "PAGE1", "changes": [
        {"field": "feed", "value": {"item": "comment", "verb": "add", "comment_id": "POST_C1",
                                    "post_id": "PAGE1_POST", "parent_id": "PAGE1_POST",
                                    "message": "Price?", "from": {"id": "U1", "name": "Rahim"},
                                    "created_time": 1732280000}},
        {"field": "feed", "value": {"item": "comment", "verb": "add", "comment_id": "POST_C2",
                                    "post_id": "PAGE1_POST", "parent_id": "POST_C1",
                                    "message": "Same question", "from": {"id": "U2", "name": "Karim"}}},
        {"field": "feed", "value": {"item": "comment", "verb": "add", "comment_id": "POST_C3",
                                    "post_id": "PAGE1_POST", "message": "Thanks",
                                    "from": {"id": "PAGE1", "name": "Shop"}}},
        {"field": "feed", "value": {"item": "comment", "verb": "remove", "comment_id": "X"}},
        {"field": "feed", "value": {"item": "reaction", "verb": "add"}},
        {"field": "feed", "value": "garbage"},
    ]}]}
    evs = fb.parse_webhook(payload)
    assert len(evs) == 3
    c1, c2, c3 = evs
    assert (c1.kind, c1.thread_external_id, c1.parent_external_id, c1.author_name) == \
        ("comment", "POST_C1", "PAGE1_POST", "Rahim")
    assert c2.thread_external_id == "POST_C1" and c2.external_message_id == "POST_C2"
    assert c3.is_echo


@pytest.mark.parametrize("payload", [
    None, [], "x", {}, {"object": "instagram"}, {"object": "page"}, {"object": "page", "entry": None},
    {"object": "page", "entry": ["x", {"id": "P", "messaging": [None, 1, {"message": "x"}]}]},
    {"object": "page", "entry": [{"id": "P", "changes": [None, {"field": "feed"}]}]},
])
def test_parse_malformed(fb, payload):
    assert fb.parse_webhook(payload) == []


def test_parse_truncates_text(fb):
    payload = {"object": "page", "entry": [{"id": "P", "messaging": [
        {"sender": {"id": "U"}, "recipient": {"id": "P"}, "message": {"mid": "m", "text": "a" * 9000}}]}]}
    assert len(fb.parse_webhook(payload)[0].text) == 5000


def dm_target(age: timedelta, human: bool):
    return ReplyTarget(kind="dm", thread_external_id="USER9",
                       last_customer_message_at=NOW - age, sent_by_human=human)


@respx.mock
async def test_send_dm_within_24h(fb):
    route = respx.post(f"{G}/me/messages").mock(return_value=httpx.Response(
        200, json={"recipient_id": "USER9", "message_id": "m_out"}))
    res = await fb.send_reply(acct(), dm_target(timedelta(hours=2), human=False), "Hello")
    assert res.ok and res.external_message_id == "m_out"
    body = json.loads(route.calls.last.request.content)
    assert body == {"recipient": {"id": "USER9"}, "message": {"text": "Hello"}, "messaging_type": "RESPONSE"}


@respx.mock
async def test_send_dm_human_agent_tag(fb):
    route = respx.post(f"{G}/me/messages").mock(return_value=httpx.Response(200, json={"message_id": "m2"}))
    res = await fb.send_reply(acct(), dm_target(timedelta(days=3), human=True), "Sorry for the delay")
    assert res.ok
    body = json.loads(route.calls.last.request.content)
    assert body["messaging_type"] == "MESSAGE_TAG" and body["tag"] == "HUMAN_AGENT"


@respx.mock
async def test_send_dm_ai_blocked_after_24h(fb):
    route = respx.post(f"{G}/me/messages")
    res = await fb.send_reply(acct(), dm_target(timedelta(days=3), human=False), "x")
    assert not res.ok and res.error == "outside_messaging_window" and not res.retryable
    assert not route.called


@respx.mock
async def test_send_dm_blocked_after_7d(fb):
    res = await fb.send_reply(acct(), dm_target(timedelta(days=8), human=True), "x")
    assert not res.ok and res.error == "outside_messaging_window"


async def test_send_dm_no_last_message(fb):
    t = ReplyTarget(kind="dm", thread_external_id="U", last_customer_message_at=None, sent_by_human=True)
    res = await fb.send_reply(acct(), t, "x")
    assert not res.ok and res.error == "outside_messaging_window"


@respx.mock
async def test_send_comment_reply(fb):
    route = respx.post(f"{G}/POST_C1/comments").mock(return_value=httpx.Response(200, json={"id": "POST_C9"}))
    t = ReplyTarget(kind="comment", thread_external_id="POST_C1", last_customer_message_at=None,
                    sent_by_human=False, reply_to_external_message_id="POST_C2")
    res = await fb.send_reply(acct(), t, "It's 500 BDT")
    assert res.ok and res.external_message_id == "POST_C9"
    assert parse_qs(route.calls.last.request.content.decode())["message"] == ["It's 500 BDT"]


@respx.mock
@pytest.mark.parametrize("status,body,reconnect,retry", [
    (400, {"error": {"type": "OAuthException", "code": 190}}, True, False),
    (401, {"error": {"type": "OAuthException", "code": 100}}, True, False),
    (429, {"error": {"type": "x", "code": 1}}, False, True),
    (400, {"error": {"type": "OAuthException", "code": 613}}, False, True),
    (400, {"error": {"type": "OAuthException", "code": 4}}, False, True),
    (500, "not json", False, True),
    (400, {"error": {"type": "OAuthException", "code": 10}}, False, False),
])
async def test_send_error_mapping(fb, status, body, reconnect, retry):
    kw = {"json": body} if isinstance(body, dict) else {"text": body}
    respx.post(f"{G}/me/messages").mock(return_value=httpx.Response(status, **kw))
    res = await fb.send_reply(acct(), dm_target(timedelta(hours=1), False), "hi")
    assert not res.ok
    assert res.needs_reconnect is reconnect
    assert res.retryable is retry
    assert "PAGETOKEN" not in (res.error or "")


@respx.mock
async def test_send_network_error_retryable(fb):
    respx.post(f"{G}/me/messages").mock(side_effect=httpx.ConnectError("boom"))
    res = await fb.send_reply(acct(), dm_target(timedelta(hours=1), False), "hi")
    assert not res.ok and res.retryable


async def test_send_empty_text(fb):
    res = await fb.send_reply(acct(), dm_target(timedelta(hours=1), False), "   ")
    assert not res.ok and res.error == "empty_text"


@respx.mock
async def test_on_connected_creates_app_webhook_subscription():
    from urllib.parse import parse_qs
    sub = respx.post(f"{G}/fbapp/subscriptions").mock(return_value=httpx.Response(200, json={"success": True}))
    respx.post(f"{G}/P1/subscribed_apps").mock(return_value=httpx.Response(200, json={"success": True}))
    await FacebookAdapter().on_connected(AccountGrant(external_id="P1", name="x", tokens=TokenBundle(access_token="pt")))
    assert sub.called
    body = parse_qs(sub.calls.last.request.content.decode())
    assert body["object"] == ["page"] and "feed" in body["fields"][0] and "messages" in body["fields"][0]
    assert body["callback_url"][0].endswith("/webhooks/facebook")


@respx.mock
async def test_app_subscription_failure_does_not_block_connect():
    respx.post(f"{G}/fbapp/subscriptions").mock(return_value=httpx.Response(400, json={"error": {"code": 100}}))
    page = respx.post(f"{G}/P1/subscribed_apps").mock(return_value=httpx.Response(200, json={"success": True}))
    await FacebookAdapter().on_connected(AccountGrant(external_id="P1", name="x", tokens=TokenBundle(access_token="pt")))
    assert page.called


@respx.mock
async def test_exchange_uses_granular_scopes_when_me_accounts_empty():
    respx.get(f"{G}/oauth/access_token").mock(side_effect=[
        httpx.Response(200, json={"access_token": "short"}), httpx.Response(200, json={"access_token": "long"})])
    respx.get(f"{G}/me/accounts").mock(return_value=httpx.Response(200, json={"data": []}))
    respx.get(f"{G}/debug_token").mock(return_value=httpx.Response(200, json={"data": {"granular_scopes": [
        {"scope": "pages_messaging", "target_ids": ["395688656962311"]},
        {"scope": "pages_show_list", "target_ids": ["395688656962311"]},
        {"scope": "business_management", "target_ids": ["1462419934965768"]}]}}))
    page = respx.get(f"{G}/395688656962311").mock(return_value=httpx.Response(200, json={
        "id": "395688656962311", "name": "Baten", "access_token": "PAGE-TOKEN"}))
    grants = await FacebookAdapter().exchange_code("CODE", "https://x/cb")
    assert [g.external_id for g in grants] == ["395688656962311"]
    assert grants[0].tokens.access_token == "PAGE-TOKEN" and grants[0].name == "Baten"
    assert page.call_count == 1
