"""Regression tests for the independent security review findings (SEC-1 … SEC-9)."""
import pytest
from sqlalchemy import select

from app.ai import engine
from app.ai.providers import LLMResult
from app.core.config import Settings
from app.core.db import SessionLocal
from app.core.security import ensure_safe_config
from app.models import Job
from app.services import queue
from app.services.ingest import ingest_event
from app.services.util import now_utc
from tests.core.conftest import connect, event, make_client, register


# SEC-1: logout must invalidate the session token server-side
def test_logout_revokes_session_token(client):
    register(client)
    stolen = client.cookies.get("qms_session")
    assert client.post("/api/auth/logout").status_code == 200
    client.cookies.clear()
    client.cookies.set("qms_session", stolen)
    assert client.get("/api/auth/me").status_code == 401
    # a fresh login still works
    client.cookies.clear()
    r = client.post("/api/auth/login", json={"email": "owner@a.com", "password": "password123"})
    assert r.status_code == 200 and client.get("/api/auth/me").status_code == 200


# SEC-2: reconnecting a Page released by workspace A from workspace B must not wipe A's history
def test_claiming_released_account_keeps_other_workspace_history(app, fake, db):
    with make_client(app) as a, make_client(app) as b:
        wa = register(a, "a@a.com", "A")
        wb = register(b, "b@b.com", "B")
        connect(a, wa)
        cid = ingest_event(db, event(text="old private question")).conversation_id
        acct = a.get(f"/api/w/{wa}/connections").json()[0]
        assert a.delete(f"/api/w/{wa}/connections/{acct['id']}").status_code == 200
        assert "connected=fake" in connect(b, wb)
        assert a.get(f"/api/w/{wa}/conversations/{cid}").status_code == 200
        assert a.get(f"/api/w/{wa}/conversations").json()["total"] == 1
        assert b.get(f"/api/w/{wb}/conversations").json()["total"] == 0
        # new traffic for the Page now lands in B only
        ingest_event(db, event(text="new question"))
        assert b.get(f"/api/w/{wb}/conversations").json()["total"] == 1
        assert a.get(f"/api/w/{wa}/conversations").json()["total"] == 1


class _InjectedProvider:
    def __init__(self, answer):
        self.answer = answer

    async def generate(self, system, user, ctx):
        return LLMResult(self.answer, 0.99, False, "ok")


def _kb(client, wid, content):
    assert client.post(f"/api/w/{wid}/knowledge", json={"title": "Delivery", "content": content}).status_code == 200


# SEC-3: an injected link / phone number that is not in the KB must never be auto-sent
@pytest.mark.parametrize("answer", [
    "Delivery is 60 taka. Pay at http://evil.example.com/pay now.",
    "Delivery is 60 taka. Pay via bkash-pay.xyz",
    "Delivery is 60 taka. Send your card number to scam@evil.io",
    "Delivery is 60 taka. Call +880 1999-123456 to pay first.",
])
def test_output_guard_blocks_unverified_contact(client, fake, monkeypatch, answer):
    wid = register(client)
    _kb(client, wid, "Delivery charge inside Dhaka is 60 taka. Call 01711-000000 for help. See https://shop.example.com/faq")
    monkeypatch.setattr(engine, "get_provider", lambda: _InjectedProvider(answer))
    r = client.post(f"/api/w/{wid}/knowledge/test", json={"question": "delivery charge Dhaka?"}).json()
    assert r["needs_human"] is True and r["would_auto_reply"] is False
    assert r["reason"] == "output_guard:unverified_contact"


def test_output_guard_allows_contacts_from_kb(client, fake, monkeypatch):
    wid = register(client)
    _kb(client, wid, "Delivery charge inside Dhaka is 60 taka. Call 01711-000000 for help. See https://shop.example.com/faq")
    monkeypatch.setattr(engine, "get_provider", lambda: _InjectedProvider(
        "Delivery is 60 taka. Call 01711-000000 or see https://shop.example.com/faq."))
    r = client.post(f"/api/w/{wid}/knowledge/test", json={"question": "delivery charge Dhaka?"}).json()
    assert r["needs_human"] is False


# SEC-3b: escalation keywords must win over the small-talk shortcut
def test_escalation_keyword_beats_small_talk(client, fake):
    wid = register(client)
    assert client.put(f"/api/w/{wid}/settings", json={"escalation_keywords": ["thank"]}).status_code == 200
    r = client.post(f"/api/w/{wid}/knowledge/test", json={"question": "thank you"}).json()
    assert r["needs_human"] is True and r["would_auto_reply"] is False


# SEC-4: two workers racing for the same job — only one may claim it
def test_job_claim_is_atomic(fake):
    db1, db2 = SessionLocal(), SessionLocal()
    try:
        queue.enqueue(db1, "send_reply", {"message_id": 1})
        db1.commit()
        orig = db2.scalar
        raced = {}

        def racing_scalar(stmt, *a, **k):
            job = orig(stmt, *a, **k)
            if job is not None and not raced:
                db2.commit()  # release SQLite's write lock; db2 keeps its (now stale) pending job object
                raced["winner"] = queue.claim_next(db1)  # other worker claims between SELECT and UPDATE
            return job

        db2.scalar = racing_scalar
        loser = queue.claim_next(db2)
        assert raced["winner"] is not None and raced["winner"].status == "running"
        assert loser is None
        assert db1.scalar(select(Job.attempts).where(Job.id == raced["winner"].id)) == 1
    finally:
        db1.close()
        db2.close()


# SEC-4b: a job that keeps going stale is not re-queued forever
def test_stale_job_stops_after_max_attempts(fake, db):
    from datetime import timedelta
    job = queue.enqueue(db, "send_reply", {"message_id": 1})
    job.status, job.attempts = "running", queue.MAX_ATTEMPTS
    job.run_after = now_utc() - timedelta(hours=1)
    db.commit()
    assert queue.claim_next(db) is None
    db.refresh(job)
    assert job.status == "failed"


# SEC-5: bcrypt silently truncates at 72 bytes — reject longer passwords at registration
def test_register_rejects_password_over_72_bytes(client):
    r = client.post("/api/auth/register", json={"email": "u@a.com", "password": "é" * 40, "workspace_name": "W"})
    assert r.status_code == 422


# SEC-6: registration is rate-limited per IP (slows account enumeration / signup spam)
def test_register_rate_limited(client):
    codes = [client.post("/api/auth/register", json={"email": f"u{i}@a.com", "password": "password123",
                                                     "workspace_name": "W"}).status_code for i in range(21)]
    assert codes[:20] == [200] * 20 and codes[20] == 429


# SEC-7: insecure config that silently disables production checks / opens CORS is refused
def test_unknown_env_and_wildcard_cors_refused():
    with pytest.raises(RuntimeError):
        ensure_safe_config(Settings(env="production"))
    with pytest.raises(RuntimeError):
        ensure_safe_config(Settings(env="dev", cors_origins="*"))
    with pytest.raises(RuntimeError):
        ensure_safe_config(Settings(env="dev", cors_origins="https://a.example, *"))
    ensure_safe_config(Settings(env="dev", cors_origins="https://a.example"))


# SEC-8: oversized / unbounded API bodies are rejected before parsing
def test_api_body_size_limit(client, fake):
    wid = register(client)
    big = b'{"title": "t", "content": "' + b"a" * 2_100_000 + b'"}'
    r = client.post(f"/api/w/{wid}/knowledge", content=big, headers={"Content-Type": "application/json"})
    assert r.status_code == 413

    def gen():
        yield b'{"title": "t", "content": "x"}'
    r = client.post(f"/api/w/{wid}/knowledge", content=gen(), headers={"Content-Type": "application/json"})
    assert r.status_code == 411
    # normal-size requests are unaffected
    assert client.post(f"/api/w/{wid}/knowledge", json={"title": "t", "content": "x"}).status_code == 200


# SEC-9 (existing behaviour, pinned): docs/openapi are off in prod
def test_docs_disabled_in_prod(monkeypatch):
    from app.core.config import get_settings
    monkeypatch.setenv("ENV", "prod")
    monkeypatch.setenv("SECRET_KEY", "x" * 40)
    monkeypatch.setenv("TOKEN_ENCRYPTION_KEY", "k")
    monkeypatch.setenv("COOKIE_SECURE", "true")
    get_settings.cache_clear()
    from app.main import create_app
    c = make_client(create_app())
    assert c.get("/docs").status_code == 404 and c.get("/openapi.json").status_code == 404
