import hashlib
import hmac
import json
from datetime import timedelta

from sqlalchemy import select

from app.adapters.base import SendResult
from app.models import ConnectedAccount, Conversation, Job, Message
from app.services.ingest import ingest_event
from app.services.util import now_utc

from .conftest import WEBHOOK_SECRET, connect, event, register, run_jobs

KB = ("Delivery & shipping\n\nDelivery inside Dhaka takes 2 days and costs 60 taka. "
      "Delivery outside Dhaka takes 3 to 5 days and costs 120 taka.\n\n"
      "Return policy: products can be returned within 7 days with the receipt.")


def setup_ws(client, fake, kb=True):
    wid = register(client)
    connect(client, wid)
    if kb:
        assert client.post(f"/api/w/{wid}/knowledge", json={"title": "Shipping FAQ", "content": KB}).status_code == 200
    return wid


def _conv(db, mid):
    db.expire_all()
    return db.get(Conversation, db.get(Message, mid).conversation_id)


async def test_high_confidence_auto_reply(client, fake, db):
    setup_ws(client, fake)
    msg = ingest_event(db, event(text="How many days does delivery inside Dhaka take?"))
    await run_jobs()
    conv = _conv(db, msg.id)
    assert conv.status == "auto_replied"
    out = db.scalar(select(Message).where(Message.conversation_id == conv.id, Message.direction == "outbound"))
    assert out.delivery_status == "sent" and out.author == "ai" and "2 days" in out.text
    assert fake.sent and fake.sent[0][0] == "tok-SECRET"  # decrypted token handed to adapter
    assert out.ai_sources and out.ai_sources[0]["title"] == "Shipping FAQ"


async def test_low_confidence_goes_to_human(client, fake, db):
    setup_ws(client, fake)
    msg = ingest_event(db, event(text="Do you sell laptops with warranty?"))
    await run_jobs()
    conv = _conv(db, msg.id)
    assert conv.status == "pending_human"
    assert not fake.sent


async def test_escalation_keyword_creates_draft(client, fake, db):
    wid = setup_ws(client, fake)
    msg = ingest_event(db, event(text="I want a refund, delivery outside Dhaka took 10 days"))
    await run_jobs()
    conv = _conv(db, msg.id)
    assert conv.status == "pending_human" and not fake.sent
    draft = db.scalar(select(Message).where(Message.conversation_id == conv.id, Message.delivery_status == "draft"))
    assert draft is not None
    # human approves the draft (edited) → sent
    r = client.post(f"/api/w/{wid}/conversations/{conv.id}/drafts/{draft.id}/approve", json={"text": "Sorry! We'll process it."})
    assert r.status_code == 200 and r.json()["delivery_status"] == "queued"
    await run_jobs()
    assert fake.sent[-1][2] == "Sorry! We'll process it." and fake.sent[-1][1].sent_by_human


async def test_ingest_dedupe_and_echo(client, fake, db):
    setup_ws(client, fake)
    e = event(external_message_id="dup-1", text="hello there")
    assert ingest_event(db, e) is not None
    assert ingest_event(db, e) is None
    assert ingest_event(db, event(author_external_id="acct-1", text="own")) is None
    assert ingest_event(db, event(is_echo=True, text="echo")) is None
    assert ingest_event(db, event(account_external_id="unknown", text="x")) is None
    assert db.scalar(select(Job).where(Job.type == "generate_reply")) is not None
    assert len(db.scalars(select(Message)).all()) == 1


async def test_resolved_conversation_reopens(client, fake, db):
    wid = setup_ws(client, fake)
    m1 = ingest_event(db, event(text="Do you sell laptops?"))
    await run_jobs()
    conv = _conv(db, m1.id)
    client.post(f"/api/w/{wid}/conversations/{conv.id}/status", json={"status": "resolved"})
    db.expire_all()
    ingest_event(db, event(text="hello? anyone"))
    assert _conv(db, m1.id).status in ("open", "pending_human")


async def test_auto_reply_disabled(client, fake, db):
    wid = setup_ws(client, fake)
    client.put(f"/api/w/{wid}/settings", json={"auto_reply_enabled": False})
    msg = ingest_event(db, event(text="How many days does delivery inside Dhaka take?"))
    await run_jobs()
    # draft-only mode: nothing is sent, but a confident AI draft waits for a human
    conv = _conv(db, msg.id)
    assert conv.status == "pending_human" and not fake.sent
    draft = db.scalar(select(Message).where(Message.direction == "outbound"))
    assert draft is not None and draft.delivery_status == "draft"


async def test_send_failure_retry_then_success(client, fake, db):
    setup_ws(client, fake)
    fake.results = [SendResult(ok=False, error="rate_limited", retryable=True)]
    msg = ingest_event(db, event(text="How many days does delivery inside Dhaka take?"))
    await run_jobs()
    out = db.scalar(select(Message).where(Message.direction == "outbound"))
    db.refresh(out)
    assert out.delivery_status == "queued"
    job = db.scalar(select(Job).where(Job.type == "send_reply"))
    assert job.status == "pending" and job.attempts == 1
    job.run_after = now_utc() - timedelta(seconds=1)
    db.commit()
    await run_jobs()
    db.refresh(out)
    assert out.delivery_status == "sent"


async def test_send_needs_reconnect(client, fake, db):
    setup_ws(client, fake)
    fake.results = [SendResult(ok=False, error="token expired", needs_reconnect=True)]
    msg = ingest_event(db, event(text="How many days does delivery inside Dhaka take?"))
    await run_jobs()
    db.expire_all()
    acct = db.scalar(select(ConnectedAccount))
    out = db.scalar(select(Message).where(Message.direction == "outbound"))
    assert acct.status == "needs_reconnect" and out.delivery_status == "failed"
    assert _conv(db, msg.id).status == "pending_human"


async def test_send_permanent_failure(client, fake, db):
    setup_ws(client, fake)
    fake.results = [SendResult(ok=False, error="bad_request")]
    ingest_event(db, event(text="How many days does delivery inside Dhaka take?"))
    await run_jobs()
    out = db.scalar(select(Message).where(Message.direction == "outbound"))
    db.refresh(out)
    assert out.delivery_status == "failed" and out.error == "bad_request"


async def test_dm_window_rules(client, fake, db):
    wid = setup_ws(client, fake)
    old = now_utc() - timedelta(hours=30)
    msg = ingest_event(db, event(text="How many days does delivery inside Dhaka take?", timestamp=old))
    await run_jobs()
    conv = _conv(db, msg.id)
    assert conv.status == "pending_human" and not fake.sent  # AI blocked after 24h
    d = client.get(f"/api/w/{wid}/conversations/{conv.id}").json()
    assert d["reply_window"]["dm_ai_allowed"] is False and d["reply_window"]["dm_human_allowed"] is True
    r = client.post(f"/api/w/{wid}/conversations/{conv.id}/reply", json={"text": "Hi, 2 days inside Dhaka."})
    assert r.status_code == 200
    await run_jobs()
    assert fake.sent and fake.sent[-1][1].sent_by_human

    # after 7 days even humans are blocked
    conv.last_customer_message_at = now_utc() - timedelta(days=8)
    db.commit()
    r = client.post(f"/api/w/{wid}/conversations/{conv.id}/reply", json={"text": "late"})
    assert r.status_code == 409 and r.json()["detail"] == "outside_messaging_window"


async def test_comment_not_window_limited(client, fake, db):
    setup_ws(client, fake)
    old = now_utc() - timedelta(days=30)
    msg = ingest_event(db, event(kind="comment", thread_external_id="c-1", parent_external_id="post-1",
                                 text="delivery inside Dhaka days?", timestamp=old))
    await run_jobs()
    assert _conv(db, msg.id).status == "auto_replied"
    assert fake.sent[0][1].kind == "comment" and fake.sent[0][1].parent_external_id == "post-1"


async def test_webhook_verify_and_signature(client, fake, db):
    setup_ws(client, fake)
    ok = client.get("/webhooks/fake", params={"hub.mode": "subscribe", "hub.verify_token": "verify-me",
                                              "hub.challenge": "12345"})
    assert ok.status_code == 200 and ok.text == "12345"
    bad = client.get("/webhooks/fake", params={"hub.mode": "subscribe", "hub.verify_token": "nope",
                                               "hub.challenge": "1"})
    assert bad.status_code == 403
    body = json.dumps({"events": [{"text": "How many days does delivery inside Dhaka take?",
                                   "external_message_id": "wh-1"}]}).encode()
    r = client.post("/webhooks/fake", content=body, headers={"X-Hub-Signature-256": "sha256=deadbeef"})
    assert r.status_code == 403
    sig = "sha256=" + hmac.new(WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
    r = client.post("/webhooks/fake", content=body, headers={"X-Hub-Signature-256": sig})
    assert r.status_code == 200 and r.json()["stored"] == 1
    # webhooks are not CSRF-protected (no cookie) and too-large bodies are refused
    big = b"{" + b" " * 1_000_001 + b"}"
    assert client.post("/webhooks/fake", content=big, headers={"X-Hub-Signature-256": sig}).status_code == 413
    assert client.post("/webhooks/unknown", content=body).status_code == 404


async def test_polling(client, fake, db):
    from app.services.polling import schedule_polls
    wid = register(client)
    connect(client, wid, platform="fakepoll")
    client.post(f"/api/w/{wid}/knowledge", json={"title": "Shipping FAQ", "content": KB})
    fake.poller.poll_events = [event(platform="fakepoll", kind="comment", thread_external_id="yt-c1",
                                     text="delivery inside Dhaka days?")]
    assert schedule_polls(db) == 1
    assert schedule_polls(db) == 0  # already pending
    await run_jobs()
    db.expire_all()
    acct = db.scalar(select(ConnectedAccount).where(ConnectedAccount.platform == "fakepoll"))
    assert acct.meta["cursor"] == "c1" and acct.last_polled_at is not None
    assert fake.poller.sent  # ingested + auto-replied
    assert schedule_polls(db) == 0  # interval not elapsed


async def test_sandbox_simulate_end_to_end(client, fake, db):
    wid = register(client)
    assert client.post(f"/api/w/{wid}/sandbox/simulate", json={"text": "hi"}).status_code == 400
    connect(client, wid, platform="sandbox")
    client.post(f"/api/w/{wid}/knowledge", json={"title": "Shipping FAQ", "content": KB})
    r = client.post(f"/api/w/{wid}/sandbox/simulate", json={"kind": "dm", "text": "Delivery inside Dhaka koto din lage? days?",
                                                           "author_name": "Karim"})
    assert r.status_code == 200
    await run_jobs()
    detail = client.get(f"/api/w/{wid}/conversations/{r.json()['conversation_id']}").json()
    assert [m["author"] for m in detail["messages"]] == ["customer", "ai"]
    stats = client.get(f"/api/w/{wid}/stats").json()
    assert stats["auto_replied_24h"] == 1
    lst = client.get(f"/api/w/{wid}/conversations", params={"q": "Karim"}).json()
    assert lst["total"] == 1 and lst["items"][0]["contact"]["display_name"] == "Karim"


async def test_regenerate_after_kb_update(client, fake, db):
    wid = setup_ws(client, fake)
    client.put(f"/api/w/{wid}/settings", json={"auto_reply_enabled": False})
    msg = ingest_event(db, event(text="How many days does delivery inside Dhaka take?"))
    await run_jobs()
    conv = _conv(db, msg.id)
    r = client.post(f"/api/w/{wid}/conversations/{conv.id}/regenerate")
    assert r.status_code == 200, r.text
    await run_jobs()
    drafts = db.scalars(select(Message).where(Message.conversation_id == conv.id, Message.direction == "outbound")).all()
    assert len(drafts) == 1 and drafts[0].delivery_status == "draft"
