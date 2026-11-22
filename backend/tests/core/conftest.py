import hashlib
import hmac
import itertools
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.adapters.base import (AccountGrant, InboundEvent, PlatformAdapter, PollResult, SendResult,
                               TokenBundle)
from app.core.config import get_settings
from app.core.db import SessionLocal, create_all, init_engine
from app.core.security import login_ip_limiter, login_limiter, register_ip_limiter
from app.services import registry

WEBHOOK_SECRET = "s3cret"
_ids = itertools.count(1)


class FakeAdapter(PlatformAdapter):
    platform = "fake"
    label = "Fake"
    supports_dm = True
    supports_comments = True
    uses_webhooks = True

    def __init__(self, platform="fake", uses_webhooks=True):
        self.platform = platform
        self.uses_webhooks = uses_webhooks
        self.sent: list[tuple] = []
        self.results: list[SendResult] = []  # queued results for send_reply; default ok
        self.grants: list[AccountGrant] | None = None
        self.connected: list[str] = []
        self.poll_events: list[InboundEvent] = []
        self.fail_on_connect = False

    def is_configured(self):
        return True

    def authorize_url(self, state, redirect_uri):
        return f"https://fake.example/auth?state={state}&redirect_uri={redirect_uri}"

    async def exchange_code(self, code, redirect_uri):
        if code == "bad":
            raise RuntimeError("boom")
        if self.grants is not None:
            return self.grants
        return [AccountGrant(external_id="acct-1", name="Fake Page", tokens=TokenBundle(access_token="tok-SECRET"))]

    async def on_connected(self, grant):
        if self.fail_on_connect:
            from app.adapters.base import AdapterError
            raise AdapterError("subscribe failed")
        self.connected.append(grant.external_id)

    def verify_token_matches(self, token):
        return token == "verify-me"

    def verify_webhook_signature(self, body, headers):
        sig = headers.get("X-Hub-Signature-256") or ""
        expected = "sha256=" + hmac.new(WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(sig, expected)

    def parse_webhook(self, payload):
        return [event(**e) for e in payload.get("events", [])]

    async def poll(self, account, since):
        evs, self.poll_events = self.poll_events, []
        return PollResult(events=evs, meta_updates={"cursor": "c1"})

    async def send_reply(self, account, target, text):
        self.sent.append((account.access_token, target, text))
        if self.results:
            return self.results.pop(0)
        return SendResult(ok=True, external_message_id=f"out-{next(_ids)}")

    def make_event(self, account_external_id, kind, text, author_name):
        n = next(_ids)
        return event(platform=self.platform, account_external_id=account_external_id, kind=kind, text=text,
                     author_name=author_name, external_message_id=f"sbx-{n}",
                     thread_external_id=f"user-{author_name}" if kind == "dm" else f"c-{n}",
                     author_external_id=f"user-{author_name}")


def event(**kw) -> InboundEvent:
    base = dict(platform="fake", account_external_id="acct-1", kind="dm", external_message_id=f"m-{next(_ids)}",
                thread_external_id="cust-1", author_external_id="cust-1", author_name="Rahim",
                text="hello", timestamp=datetime.now(timezone.utc))
    base.update(kw)
    if isinstance(base["timestamp"], str):
        base["timestamp"] = datetime.fromisoformat(base["timestamp"])
    return InboundEvent(**base)


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("ENV", "test")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/test.db")
    monkeypatch.setenv("WORKER_ENABLED", "false")
    monkeypatch.setenv("SECRET_KEY", "test-secret-key-that-is-long-enough-1234")
    monkeypatch.setenv("SANDBOX_ENABLED", "true")
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    monkeypatch.setenv("EMBEDDINGS", "none")
    monkeypatch.setenv("PUBLIC_BASE_URL", "http://testserver")
    get_settings.cache_clear()
    init_engine(f"sqlite:///{tmp_path}/test.db")
    create_all()
    login_limiter.reset()
    login_ip_limiter.reset()
    register_ip_limiter.reset()
    yield
    registry.set_override(None)
    get_settings.cache_clear()


@pytest.fixture
def fake():
    a = FakeAdapter("fake")
    sbx = FakeAdapter("sandbox")
    poller = FakeAdapter("fakepoll", uses_webhooks=False)
    registry.set_override([a, sbx, poller])
    a.sandbox, a.poller = sbx, poller
    return a


@pytest.fixture
def app():
    from app.main import create_app
    return create_app()


def make_client(app) -> TestClient:
    return TestClient(app, base_url="http://testserver")


def register(client, email="owner@a.com", ws="Acme", password="password123"):
    r = client.post("/api/auth/register", json={"email": email, "password": password, "name": "Owner",
                                                "workspace_name": ws})
    assert r.status_code == 200, r.text
    client.headers["X-CSRF-Token"] = client.cookies.get("qms_csrf")
    return r.json()["workspaces"][0]["id"]


@pytest.fixture
def client(app):
    with make_client(app) as c:
        yield c


@pytest.fixture
def db():
    s = SessionLocal()
    yield s
    s.close()


def connect(client, wid, platform="fake", code="ok"):
    """Run the full OAuth connect flow against the fake adapter."""
    r = client.post(f"/api/w/{wid}/connections/{platform}/start")
    assert r.status_code == 200, r.text
    state = r.json()["authorize_url"].split("state=")[1].split("&")[0]
    r = client.get(f"/oauth/callback/{platform}", params={"code": code, "state": state}, follow_redirects=False)
    assert r.status_code == 302
    return r.headers["location"]


async def run_jobs(n=50):
    from app.worker.runner import process_due_jobs
    return await process_due_jobs(max_jobs=n)
