"""Smoke test against the real adapter registry (sandbox platform, no fakes)."""
from .conftest import register, run_jobs


async def test_real_sandbox_flow(client):
    wid = register(client)
    plats = {p["platform"]: p for p in client.get(f"/api/w/{wid}/platforms").json()}
    assert {"facebook", "instagram", "youtube", "sandbox"} <= set(plats)
    assert plats["sandbox"]["configured"]
    r = client.post(f"/api/w/{wid}/connections/sandbox/start")
    assert r.status_code == 200
    url = r.json()["authorize_url"]
    path = url.replace("http://testserver", "")
    r = client.get(path, follow_redirects=False)
    assert r.status_code == 302 and "connected=sandbox" in r.headers["location"], r.headers["location"]
    client.post(f"/api/w/{wid}/knowledge", json={"title": "Hours", "content": "We are open from 10am to 8pm every day."})
    r = client.post(f"/api/w/{wid}/sandbox/simulate", json={"kind": "comment", "text": "What are your opening hours?"})
    assert r.status_code == 200, r.text
    await run_jobs()
    d = client.get(f"/api/w/{wid}/conversations/{r.json()['conversation_id']}").json()
    assert d["messages"][-1]["author"] == "ai" and d["messages"][-1]["delivery_status"] == "sent"
    # unconfigured real platforms refuse to start OAuth
    if not plats["facebook"]["configured"]:
        assert client.post(f"/api/w/{wid}/connections/facebook/start").status_code == 400
