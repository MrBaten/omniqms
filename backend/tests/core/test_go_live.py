"""Readiness for real Meta testing: Business-login config_id and public legal pages."""
import httpx

from app.adapters.facebook import FacebookAdapter
from app.core.config import get_settings


def test_facebook_business_login_uses_config_id(monkeypatch):
    monkeypatch.setenv("META_APP_ID", "app1")
    monkeypatch.setenv("META_LOGIN_CONFIG_ID", "cfg123")
    get_settings.cache_clear()
    url = httpx.URL(FacebookAdapter().authorize_url("st", "https://x/cb"))
    assert url.params["config_id"] == "cfg123"
    assert url.params["response_type"] == "code"
    assert "scope" not in url.params
    monkeypatch.delenv("META_LOGIN_CONFIG_ID")
    get_settings.cache_clear()
    url = httpx.URL(FacebookAdapter().authorize_url("st", "https://x/cb"))
    assert "pages_messaging" in url.params["scope"] and "config_id" not in url.params


def test_legal_pages_are_public_and_escaped(client, monkeypatch):
    monkeypatch.setenv("OPERATOR_NAME", "AI Native <b>Global</b>")
    get_settings.cache_clear()
    for path in ("/privacy", "/data-deletion", "/terms"):
        r = client.get(path)
        assert r.status_code == 200 and "text/html" in r.headers["content-type"]
        assert "<b>Global</b>" not in r.text
        if path != "/data-deletion":
            assert "AI Native &lt;b&gt;Global&lt;/b&gt;" in r.text
