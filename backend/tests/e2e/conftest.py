from tests.core.conftest import env, app, client, db  # noqa: F401  (reuse fixtures)
import pytest
from app.core.config import get_settings

META = {"META_APP_ID": "fbapp", "META_APP_SECRET": "fbsecret", "META_WEBHOOK_VERIFY_TOKEN": "fbverify",
        "META_GRAPH_VERSION": "v26.0", "IG_APP_ID": "igapp", "IG_APP_SECRET": "igsecret",
        "IG_WEBHOOK_VERIFY_TOKEN": "igverify", "GOOGLE_CLIENT_ID": "gclient", "GOOGLE_CLIENT_SECRET": "gsecret"}


@pytest.fixture(autouse=True)
def meta_env(env, monkeypatch):  # noqa: F811
    for k, v in META.items():
        monkeypatch.setenv(k, v)
    get_settings.cache_clear()
    yield
