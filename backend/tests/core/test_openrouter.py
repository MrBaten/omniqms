import json

import httpx
import respx

from app.ai.providers import OpenRouterProvider, ProviderContext, get_provider
from app.core.config import get_settings


def test_openrouter_selected_only_with_key(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "openrouter")
    get_settings.cache_clear()
    assert get_provider().__class__.__name__ == "MockProvider"
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    get_settings.cache_clear()
    assert isinstance(get_provider(), OpenRouterProvider)


@respx.mock
async def test_openrouter_request_and_parse(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "openrouter")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    get_settings.cache_clear()
    route = respx.post("https://openrouter.ai/api/v1/chat/completions").mock(return_value=httpx.Response(200, json={
        "choices": [{"message": {"content": json.dumps({"answer": "Delivery charge 60 taka.", "confidence": 0.9,
                                                        "needs_human": False, "reason": "kb"})}}]}))
    import inspect
    sig = inspect.signature(ProviderContext)
    ctx = ProviderContext(**{k: None for k in sig.parameters})
    res = await get_provider().generate("sys", "user", ctx)
    assert "60 taka" in res.answer
    req = route.calls.last.request
    assert req.headers["authorization"] == "Bearer sk-or-test"
    assert json.loads(req.content)["model"] == "openai/gpt-4o-mini"
