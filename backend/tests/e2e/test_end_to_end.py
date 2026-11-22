"""End-to-end: real app + real adapters, every platform HTTP call mocked with respx."""
import hashlib, hmac, json, time
from datetime import datetime, timedelta, timezone

import httpx
import respx

from tests.core.conftest import register, run_jobs

G = "https://graph.facebook.com/v26.0"
IG = "https://graph.instagram.com/v26.0"
YT = "https://www.googleapis.com/youtube/v3"

KB = ("Q: Delivery charge koto?\nA: Dhaka city-r moddhe delivery charge 60 taka, baire 120 taka.\n\n"
      "Q: Cash on delivery ache?\nA: Haan, sara Bangladesh-e cash on delivery ache.")


def sign(secret, body: bytes):
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def oauth(client, wid, platform, code="CODE"):
    r = client.post(f"/api/w/{wid}/connections/{platform}/start")
    assert r.status_code == 200, r.text
    url = r.json()["authorize_url"]
    state = httpx.URL(url).params["state"]
    r = client.get(f"/oauth/callback/{platform}", params={"code": code, "state": state}, follow_redirects=False)
    assert r.status_code == 302, r.text
    return r.headers["location"]


def setup_ws(client):
    wid = register(client)
    r = client.post(f"/api/w/{wid}/knowledge", json={"title": "Delivery FAQ", "source_type": "faq", "content": KB})
    assert r.status_code in (200, 201), r.text
    return wid


@respx.mock
async def test_facebook_connect_webhook_autoreply(client):
    wid = setup_ws(client)
    respx.get(f"{G}/oauth/access_token").mock(side_effect=[
        httpx.Response(200, json={"access_token": "short-user"}),
        httpx.Response(200, json={"access_token": "long-user", "expires_in": 5184000})])
    respx.get(f"{G}/me/accounts").mock(return_value=httpx.Response(200, json={
        "data": [{"id": "PAGE1", "name": "Example Shop", "access_token": "PAGE-TOKEN-SECRET"}]}))
    sub = respx.post(f"{G}/PAGE1/subscribed_apps").mock(return_value=httpx.Response(200, json={"success": True}))
    loc = oauth(client, wid, "facebook")
    assert "connected=facebook" in loc
    assert sub.called
    conns = client.get(f"/api/w/{wid}/connections").json()
    assert conns[0]["external_id"] == "PAGE1" and conns[0]["status"] == "connected"
    assert "PAGE-TOKEN-SECRET" not in json.dumps(conns)

    # signed Messenger webhook
    payload = {"object": "page", "entry": [{"id": "PAGE1", "time": int(time.time() * 1000), "messaging": [{
        "sender": {"id": "CUST9"}, "recipient": {"id": "PAGE1"}, "timestamp": int(time.time() * 1000),
        "message": {"mid": "mid.1", "text": "Delivery charge koto Dhaka-r baire?"}}]}]}
    body = json.dumps(payload).encode()
    bad = client.post("/webhooks/facebook", content=body, headers={"X-Hub-Signature-256": "sha256=00"})
    assert bad.status_code == 403
    r = client.post("/webhooks/facebook", content=body, headers={"X-Hub-Signature-256": sign("fbsecret", body),
                                                                  "Content-Type": "application/json"})
    assert r.status_code == 200
    # duplicate delivery must not create a second message
    client.post("/webhooks/facebook", content=body, headers={"X-Hub-Signature-256": sign("fbsecret", body),
                                                              "Content-Type": "application/json"})
    send = respx.post(f"{G}/me/messages").mock(return_value=httpx.Response(200, json={"message_id": "out.1"}))
    await run_jobs()
    assert send.called
    sent = json.loads(send.calls.last.request.content)
    assert sent["recipient"]["id"] == "CUST9" and sent["messaging_type"] == "RESPONSE"
    assert "120" in sent["message"]["text"]
    assert send.calls.last.request.url.params.get("access_token") == "PAGE-TOKEN-SECRET" or \
        "PAGE-TOKEN-SECRET" in send.calls.last.request.headers.get("authorization", "")
    convs = client.get(f"/api/w/{wid}/conversations").json()
    assert convs["total"] == 1
    d = client.get(f"/api/w/{wid}/conversations/{convs['items'][0]['id']}").json()
    assert [m["direction"] for m in d["messages"]] == ["inbound", "outbound"]
    assert d["messages"][1]["delivery_status"] == "sent"


@respx.mock
async def test_old_dm_goes_to_human_and_human_agent_tag(client):
    wid = setup_ws(client)
    respx.get(f"{G}/oauth/access_token").mock(side_effect=[
        httpx.Response(200, json={"access_token": "s"}), httpx.Response(200, json={"access_token": "l"})])
    respx.get(f"{G}/me/accounts").mock(return_value=httpx.Response(200, json={
        "data": [{"id": "PAGE1", "name": "Shop", "access_token": "PT"}]}))
    respx.post(f"{G}/PAGE1/subscribed_apps").mock(return_value=httpx.Response(200, json={"success": True}))
    oauth(client, wid, "facebook")
    ts = int((datetime.now(timezone.utc) - timedelta(hours=30)).timestamp() * 1000)
    payload = {"object": "page", "entry": [{"id": "PAGE1", "time": ts, "messaging": [{
        "sender": {"id": "C2"}, "recipient": {"id": "PAGE1"}, "timestamp": ts,
        "message": {"mid": "mid.old", "text": "Cash on delivery ache?"}}]}]}
    body = json.dumps(payload).encode()
    client.post("/webhooks/facebook", content=body, headers={"X-Hub-Signature-256": sign("fbsecret", body),
                                                              "Content-Type": "application/json"})
    send = respx.post(f"{G}/me/messages").mock(return_value=httpx.Response(200, json={"message_id": "o2"}))
    await run_jobs()
    assert not send.called, "AI must not reply outside the 24h window"
    items = client.get(f"/api/w/{wid}/conversations", params={"status": "pending_human"}).json()["items"]
    assert len(items) == 1
    cid = items[0]["id"]
    d = client.get(f"/api/w/{wid}/conversations/{cid}").json()
    assert d["reply_window"]["dm_ai_allowed"] is False and d["reply_window"]["dm_human_allowed"] is True
    r = client.post(f"/api/w/{wid}/conversations/{cid}/reply", json={"text": "Haan, COD ache."})
    assert r.status_code in (200, 201), r.text
    await run_jobs()
    body = json.loads(send.calls.last.request.content)
    assert body["messaging_type"] == "MESSAGE_TAG" and body["tag"] == "HUMAN_AGENT"


@respx.mock
async def test_instagram_comment_reply(client):
    wid = setup_ws(client)
    respx.post("https://api.instagram.com/oauth/access_token").mock(return_value=httpx.Response(200, json={
        "access_token": "ig-short", "user_id": 1784}))
    respx.get("https://graph.instagram.com/access_token").mock(return_value=httpx.Response(200, json={
        "access_token": "IG-LONG-SECRET", "expires_in": 5183944}))
    respx.get(f"{IG}/me").mock(return_value=httpx.Response(200, json={"user_id": "1784", "username": "exampleshop"}))
    respx.post(f"{IG}/me/subscribed_apps").mock(return_value=httpx.Response(200, json={"success": True}))
    assert "connected=instagram" in oauth(client, wid, "instagram")
    payload = {"object": "instagram", "entry": [{"id": "1784", "time": int(time.time()), "changes": [{
        "field": "comments", "value": {"id": "CMT1", "text": "Cash on delivery ache?",
                                       "from": {"id": "U5", "username": "nusrat"}, "media": {"id": "MEDIA1"}}}]}]}
    body = json.dumps(payload).encode()
    r = client.post("/webhooks/instagram", content=body, headers={"X-Hub-Signature-256": sign("igsecret", body),
                                                                   "Content-Type": "application/json"})
    assert r.status_code == 200
    rep = respx.post(f"{IG}/CMT1/replies").mock(return_value=httpx.Response(200, json={"id": "R1"}))
    await run_jobs()
    assert rep.called


@respx.mock
async def test_youtube_connect_poll_reply(client):
    wid = setup_ws(client)
    respx.post("https://oauth2.googleapis.com/token").mock(return_value=httpx.Response(200, json={
        "access_token": "ya29", "refresh_token": "1//RT", "expires_in": 3599}))
    respx.get(f"{YT}/channels").mock(return_value=httpx.Response(200, json={
        "items": [{"id": "UC1", "snippet": {"title": "Example Shop"}}]}))
    assert "connected=youtube" in oauth(client, wid, "youtube")
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    respx.get(f"{YT}/commentThreads").mock(return_value=httpx.Response(200, json={"items": [{
        "id": "TH1", "snippet": {"videoId": "VID1", "topLevelComment": {"id": "TH1", "snippet": {
            "textOriginal": "Delivery charge koto?", "authorDisplayName": "Karim",
            "authorChannelId": {"value": "UCX"}, "publishedAt": now, "updatedAt": now}}}}]}))
    from app.core.db import SessionLocal
    from app.services.polling import schedule_polls
    s = SessionLocal(); schedule_polls(s); s.close()
    ins = respx.post(f"{YT}/comments").mock(return_value=httpx.Response(200, json={"id": "TH1.r1"}))
    await run_jobs()
    await run_jobs()
    assert ins.called
    assert json.loads(ins.calls.last.request.content)["snippet"]["parentId"] == "TH1"


async def test_instagram_token_refresh_job(client):
    """Long-lived IG tokens nearing expiry get refreshed by the scheduled job."""
    from app.core.db import SessionLocal
    from app.core.crypto import encrypt, decrypt
    from app.models import ConnectedAccount, Workspace
    from app.services.polling import schedule_token_refreshes
    wid = register(client)
    s = SessionLocal()
    s.add(ConnectedAccount(workspace_id=wid, platform="instagram", external_id="1784", name="x",
                           access_token_enc=encrypt("OLD"),
                           token_expires_at=datetime.now(timezone.utc) + timedelta(days=2)))
    s.commit()
    assert schedule_token_refreshes(s) == 1
    s.close()
    with respx.mock:
        respx.get("https://graph.instagram.com/refresh_access_token").mock(return_value=httpx.Response(200, json={
            "access_token": "NEW", "expires_in": 5184000}))
        await run_jobs()
    s = SessionLocal()
    acct = s.query(ConnectedAccount).filter_by(external_id="1784").one()
    assert decrypt(acct.access_token_enc) == "NEW"
    s.close()
