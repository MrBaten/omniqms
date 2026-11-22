import pytest

from app.core.config import get_settings

ENV = {
    "META_APP_ID": "fbapp",
    "META_APP_SECRET": "fbsecret",
    "META_WEBHOOK_VERIFY_TOKEN": "fbverify",
    "META_GRAPH_VERSION": "v26.0",
    "IG_APP_ID": "igapp",
    "IG_APP_SECRET": "igsecret",
    "IG_WEBHOOK_VERIFY_TOKEN": "igverify",
    "GOOGLE_CLIENT_ID": "gclient",
    "GOOGLE_CLIENT_SECRET": "gsecret",
    "PUBLIC_BASE_URL": "https://qms.example.com",
    "SANDBOX_ENABLED": "true",
}


@pytest.fixture(autouse=True)
def adapter_env(monkeypatch):
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
