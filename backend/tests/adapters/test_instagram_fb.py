"""Instagram API with Facebook Login (Page-linked) connector."""
import json
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs

import httpx
import pytest
import respx

from app.adapters import get_adapter
from app.adapters.base import AccountContext, AccountGrant, ReplyTarget, TokenBundle
from app.adapters.instagram_fb import InstagramFacebookLoginAdapter
from app.core.config import get_settings
from app.core.crypto import hmac_sha256_hex

G = "https://graph.facebook.com/v26.0"


@pytest.fixture(autouse=True)
def ig_fb_env(monkeypatch):
    monkeypatch.setenv("META_IG_LOGIN_CONFIG_ID", "igcfg")
    monkeypatch.setenv("IG_WEBHOOK_VERIFY_TOKEN", "igverify")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_registry_switches_to_facebook_login_mode():
    a = get_adapter("instagram")
    assert isinstance(a, InstagramFacebookLoginAdapter) and a.is_configured()
    url = httpx.URL(a.authorize_url("st", "https://x/cb"))
    assert url.host == "www.facebook.com" and url.params["config_id"] == "igcfg" and "scope" not in url.params


@respx.mock
async def test_exchange_finds_linked_instagram_account():
    respx.get(f"{G}/oauth/access_token").mock(side_effect=[
        httpx.Response(200, json={"access_token": "short"}), httpx.Response(200, json={"access_token": "long"})])
    respx.get(f"{G}/me/accounts").mock(return_value=httpx.Response(200, json={"data": []}))
    respx.get(f"{G}/debug_token").mock(return_value=httpx.Response(200, json={"data": {"granular_scopes": [
        {"scope": "pages_show_list", "target_ids": ["PAGE1", "PAGE2"]},
        {"scope": "instagram_basic", "target_ids": ["IG1"]}]}}))
    respx.get(f"{G}/PAGE1").mock(return_value=httpx.Response(200, json={
        "id": "PAGE1", "name": "Baten", "access_token": "PT1",
        "instagram_business_account": {"id": "IG1", "username": "baten.ai"}}))
    respx.get(f"{G}/PAGE2").mock(return_value=httpx.Response(200, json={"id": "PAGE2", "access_token": "PT2"}))
    grants = await get_adapter("instagram").exchange_code("CODE", "https://x/cb")
    assert len(grants) == 1
    g = grants[0]
    assert g.external_id == "IG1" and g.name == "baten.ai" and g.tokens.access_token == "PT1"
    assert g.meta["page_id"] == "PAGE1"


@respx.mock
async def test_on_connected_subscribes_app_and_page():
    app_sub = respx.post(f"{G}/fbapp/subscriptions").mock(return_value=httpx.Response(200, json={"success": True}))
    page_sub = respx.post(f"{G}/PAGE1/subscribed_apps").mock(return_value=httpx.Response(200, json={"success": True}))
    await get_adapter("instagram").on_connected(AccountGrant(
        external_id="IG1", name="x", tokens=TokenBundle(access_token="PT1"), meta={"page_id": "PAGE1"}))
    body = parse_qs(app_sub.calls.last.request.content.decode())
    assert body["object"] == ["instagram"] and "comments" in body["fields"][0]
    assert body["callback_url"][0].endswith("/webhooks/instagram") and page_sub.called


def test_signature_uses_meta_app_secret():
    a = get_adapter("instagram")
    body = b'{"object":"instagram","entry":[]}'
    good = {"X-Hub-Signature-256": "sha256=" + hmac_sha256_hex(get_settings().meta_app_secret, body)}
    assert a.verify_webhook_signature(body, good)
    bad = {"X-Hub-Signature-256": "sha256=" + hmac_sha256_hex("other", body)}
    assert not a.verify_webhook_signature(body, bad)


@respx.mock
async def test_dm_sent_through_linked_page_with_window_rules():
    route = respx.post(f"{G}/PAGE1/messages").mock(return_value=httpx.Response(200, json={"message_id": "m1"}))
    ctx = AccountContext(external_id="IG1", access_token="PT1", meta={"page_id": "PAGE1"})
    now = datetime.now(timezone.utc)
    res = await get_adapter("instagram").send_reply(ctx, ReplyTarget(
        kind="dm", thread_external_id="IGSID9", last_customer_message_at=now, sent_by_human=False), "Hi")
    assert res.ok and res.external_message_id == "m1"
    sent = json.loads(route.calls.last.request.content)
    assert sent["recipient"]["id"] == "IGSID9" and sent["messaging_type"] == "RESPONSE"
    late = await get_adapter("instagram").send_reply(ctx, ReplyTarget(
        kind="dm", thread_external_id="IGSID9", last_customer_message_at=now - timedelta(days=3),
        sent_by_human=False), "Hi")
    assert not late.ok and late.error == "outside_messaging_window"


@respx.mock
async def test_comment_reply_on_graph_facebook():
    route = respx.post(f"{G}/C1/replies").mock(return_value=httpx.Response(200, json={"id": "R1"}))
    ctx = AccountContext(external_id="IG1", access_token="PT1", meta={"page_id": "PAGE1"})
    res = await get_adapter("instagram").send_reply(ctx, ReplyTarget(
        kind="comment", thread_external_id="C1", last_customer_message_at=None, sent_by_human=False), "Thanks!")
    assert res.ok and route.called


def test_parses_instagram_comment_and_dm():
    a = get_adapter("instagram")
    evs = a.parse_webhook({"object": "instagram", "entry": [{"id": "IG1", "time": 1, "changes": [
        {"field": "comments", "value": {"id": "C1", "text": "price?", "from": {"id": "U1", "username": "nus"},
                                        "media": {"id": "M1"}}}], "messaging": [
        {"sender": {"id": "U2"}, "recipient": {"id": "IG1"}, "timestamp": 1, "message": {"mid": "mid1", "text": "hi"}}]}]})
    assert {(e.kind, e.account_external_id) for e in evs} == {("comment", "IG1"), ("dm", "IG1")}
