from datetime import timedelta

from app.core.security import ensure_safe_config
from app.core.config import Settings
from app.models import OAuthState
from app.services.util import now_utc

from .conftest import connect, make_client, register


def test_register_login_me_logout(client):
    wid = register(client)
    r = client.get("/api/auth/me")
    assert r.status_code == 200 and r.json()["workspaces"][0]["id"] == wid
    assert client.post("/api/auth/logout").status_code == 200
    client.cookies.clear()
    assert client.get("/api/auth/me").status_code == 401
    r = client.post("/api/auth/login", json={"email": "OWNER@a.com", "password": "password123"})
    assert r.status_code == 200
    assert client.get("/api/auth/me").status_code == 200


def test_session_cookie_flags(client):
    r = client.post("/api/auth/register", json={"email": "x@y.com", "password": "password123", "workspace_name": "W"})
    cookies = r.headers.get_list("set-cookie")
    sess = next(c for c in cookies if c.startswith("qms_session="))
    assert "HttpOnly" in sess and "samesite=lax" in sess.lower()


def test_duplicate_register_generic(client):
    register(client)
    r = client.post("/api/auth/register", json={"email": "owner@a.com", "password": "password123", "workspace_name": "B"})
    assert r.status_code == 409


def test_bad_login_and_rate_limit(client):
    register(client)
    client.cookies.clear()
    for _ in range(10):
        r = client.post("/api/auth/login", json={"email": "owner@a.com", "password": "wrong-pass"})
        assert r.status_code == 401
        assert r.json()["detail"] == "Invalid email or password"
    r = client.post("/api/auth/login", json={"email": "owner@a.com", "password": "password123"})
    assert r.status_code == 429


def test_unknown_email_same_error(client):
    r = client.post("/api/auth/login", json={"email": "nobody@a.com", "password": "whatever1"})
    assert r.status_code == 401 and r.json()["detail"] == "Invalid email or password"


def test_csrf_required(client, fake):
    wid = register(client)
    token = client.headers.pop("X-CSRF-Token")
    r = client.post(f"/api/w/{wid}/knowledge", json={"title": "t", "content": "c"})
    assert r.status_code == 403
    r = client.post(f"/api/w/{wid}/knowledge", json={"title": "t", "content": "c"}, headers={"X-CSRF-Token": "wrong"})
    assert r.status_code == 403
    r = client.post(f"/api/w/{wid}/knowledge", json={"title": "t", "content": "c"}, headers={"X-CSRF-Token": token})
    assert r.status_code == 200


def test_tampered_session_rejected(client):
    register(client)
    client.cookies.set("qms_session", client.cookies.get("qms_session") + "x")
    assert client.get("/api/auth/me").status_code == 401


def test_security_headers(client):
    r = client.get("/healthz")
    assert r.headers["X-Frame-Options"] == "DENY"
    assert "frame-ancestors 'none'" in r.headers["Content-Security-Policy"]
    assert r.headers["X-Content-Type-Options"] == "nosniff"


def test_prod_refuses_default_secret():
    import pytest
    with pytest.raises(RuntimeError):
        ensure_safe_config(Settings(env="prod"))
    ensure_safe_config(Settings(env="prod", secret_key="x" * 40, token_encryption_key="k", cookie_secure=True))


def test_tenant_isolation(app, fake, db):
    with make_client(app) as a, make_client(app) as b:
        wa = register(a, "a@a.com", "A")
        wb = register(b, "b@b.com", "B")
        connect(a, wa)
        doc = a.post(f"/api/w/{wa}/knowledge", json={"title": "Prices", "content": "Shirt costs 500 taka."}).json()
        a.post(f"/api/w/{wa}/sandbox/simulate", json={"text": "hi"})  # sandbox not connected → 400, ignore
        from app.services.ingest import ingest_event
        from .conftest import event
        msg = ingest_event(db, event(text="price of shirt?"))
        cid = msg.conversation_id
        acct_id = a.get(f"/api/w/{wa}/connections").json()[0]["id"]

        # B cannot reach A's workspace at all
        for path in ("conversations", "knowledge", "connections", "settings", "stats", "platforms"):
            assert b.get(f"/api/w/{wa}/{path}").status_code == 404
        # B cannot use A's IDs through its own workspace either
        assert b.get(f"/api/w/{wb}/conversations/{cid}").status_code == 404
        assert b.post(f"/api/w/{wb}/conversations/{cid}/reply", json={"text": "x"}).status_code == 404
        assert b.post(f"/api/w/{wb}/conversations/{cid}/status", json={"status": "resolved"}).status_code == 404
        assert b.put(f"/api/w/{wb}/knowledge/{doc['id']}", json={"title": "pwn"}).status_code == 404
        assert b.delete(f"/api/w/{wb}/knowledge/{doc['id']}").status_code == 404
        assert b.delete(f"/api/w/{wb}/connections/{acct_id}").status_code == 404
        assert b.put(f"/api/w/{wa}/settings", json={"auto_reply_enabled": False}).status_code == 404
        assert b.get(f"/api/w/{wb}/conversations").json()["total"] == 0
        assert a.get(f"/api/w/{wa}/conversations").json()["total"] == 1


def test_tokens_never_returned(client, fake):
    wid = register(client)
    connect(client, wid)
    body = client.get(f"/api/w/{wid}/connections").text
    assert "tok-SECRET" not in body and "access_token" not in body


def _start_state(client, wid, platform="fake"):
    r = client.post(f"/api/w/{wid}/connections/{platform}/start")
    return r.json()["authorize_url"].split("state=")[1].split("&")[0]


def test_oauth_state_invalid_reused_expired_wrong_platform(client, fake, db):
    wid = register(client)
    # invalid
    r = client.get("/oauth/callback/fake", params={"code": "ok", "state": "nope"}, follow_redirects=False)
    assert "error=invalid_state" in r.headers["location"]
    # success then reuse
    st = _start_state(client, wid)
    r = client.get("/oauth/callback/fake", params={"code": "ok", "state": st}, follow_redirects=False)
    assert "connected=fake" in r.headers["location"]
    r = client.get("/oauth/callback/fake", params={"code": "ok", "state": st}, follow_redirects=False)
    assert "error=invalid_state" in r.headers["location"]
    # wrong platform
    st = _start_state(client, wid, "fake")
    r = client.get("/oauth/callback/sandbox", params={"code": "ok", "state": st}, follow_redirects=False)
    assert "error=invalid_state" in r.headers["location"]
    # expired
    st = _start_state(client, wid)
    row = db.get(OAuthState, st)
    row.created_at = now_utc() - timedelta(minutes=11)
    db.commit()
    r = client.get("/oauth/callback/fake", params={"code": "ok", "state": st}, follow_redirects=False)
    assert "error=expired_state" in r.headers["location"]
    # user denied
    st = _start_state(client, wid)
    r = client.get("/oauth/callback/fake", params={"error": "access_denied", "state": st}, follow_redirects=False)
    assert "error=access_denied" in r.headers["location"]
    # exchange failure does not leak
    st = _start_state(client, wid)
    r = client.get("/oauth/callback/fake", params={"code": "bad", "state": st}, follow_redirects=False)
    assert r.headers["location"].endswith("error=exchange_failed")


def test_oauth_state_bound_to_user(app, fake):
    with make_client(app) as a, make_client(app) as b:
        wa = register(a, "a@a.com", "A")
        register(b, "b@b.com", "B")
        st = _start_state(a, wa)
        r = b.get("/oauth/callback/fake", params={"code": "ok", "state": st}, follow_redirects=False)
        assert "error=invalid_state" in r.headers["location"]
        r = make_client(app).get("/oauth/callback/fake", params={"code": "ok", "state": st}, follow_redirects=False)
        assert "error=not_authenticated" in r.headers["location"]


def test_account_in_use_by_other_workspace(app, fake):
    with make_client(app) as a, make_client(app) as b:
        wa = register(a, "a@a.com", "A")
        wb = register(b, "b@b.com", "B")
        assert "connected=fake" in connect(a, wa)
        assert "error=account_in_use" in connect(b, wb)
        # after A disconnects, B may claim it
        acct = a.get(f"/api/w/{wa}/connections").json()[0]
        assert a.delete(f"/api/w/{wa}/connections/{acct['id']}").status_code == 200
        assert "connected=fake" in connect(b, wb)


def test_on_connected_failure_marks_needs_reconnect(client, fake):
    wid = register(client)
    fake.fail_on_connect = True
    connect(client, wid)
    acct = client.get(f"/api/w/{wid}/connections").json()[0]
    assert acct["status"] == "needs_reconnect" and acct["last_error"] == "subscribe failed"
