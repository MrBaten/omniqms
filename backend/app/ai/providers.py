"""LLM providers. MockProvider works offline and is deterministic (default)."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

import httpx

from app.ai.retrieval import ScoredChunk, tokenize
from app.core.config import Settings, get_settings


@dataclass
class ProviderContext:
    question: str
    chunks: list[ScoredChunk]
    retrieval_signal: float
    kind: str
    max_chars: int
    history: list[dict] = field(default_factory=list)


@dataclass
class LLMResult:
    answer: str
    confidence: float
    needs_human: bool
    reason: str = ""


class LLMProvider:
    name = "base"

    async def generate(self, system: str, user: str, ctx: ProviderContext) -> LLMResult:  # pragma: no cover
        raise NotImplementedError


_SENT_SPLIT = re.compile(r"(?<=[.!?।])\s+|\n+")


class MockProvider(LLMProvider):
    """Extractive answer: the best-matching sentences from the top chunk."""
    name = "mock"

    async def generate(self, system: str, user: str, ctx: ProviderContext) -> LLMResult:
        if not ctx.chunks or ctx.retrieval_signal <= 0:
            return LLMResult("", 0.0, True, "no_relevant_knowledge")
        q = set(tokenize(ctx.question))
        top = ctx.chunks[0]
        sentences = [s.strip() for s in _SENT_SPLIT.split(top.text) if s.strip()]
        if top.title and sentences and sentences[0] == top.title.strip():
            sentences = sentences[1:] or sentences
        ranked = sorted(range(len(sentences)),
                        key=lambda i: (len(q & set(tokenize(sentences[i]))), -i), reverse=True)
        keep = sorted(ranked[:2])
        answer = " ".join(sentences[i] for i in keep if len(q & set(tokenize(sentences[i]))) > 0) \
            or " ".join(sentences[:2])
        return LLMResult(answer[: ctx.max_chars], ctx.retrieval_signal, False, "extractive")


def _parse_json_result(text: str) -> LLMResult:
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return LLMResult("", 0.0, True, "unparseable_model_output")
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return LLMResult("", 0.0, True, "unparseable_model_output")
    try:
        conf = float(data.get("confidence", 0))
    except (TypeError, ValueError):
        conf = 0.0
    return LLMResult(str(data.get("answer") or "").strip(), max(0.0, min(conf, 1.0)),
                     bool(data.get("needs_human", False)), str(data.get("reason") or "")[:200])


class OpenAIProvider(LLMProvider):
    name = "openai"

    def __init__(self, s: Settings):
        self.s = s

    async def generate(self, system: str, user: str, ctx: ProviderContext) -> LLMResult:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post("https://api.openai.com/v1/chat/completions",
                             headers={"Authorization": f"Bearer {self.s.openai_api_key}"},
                             json={"model": self.s.openai_model, "temperature": 0.2,
                                   "response_format": {"type": "json_object"},
                                   "messages": [{"role": "system", "content": system},
                                                {"role": "user", "content": user}]})
            r.raise_for_status()
            return _parse_json_result(r.json()["choices"][0]["message"]["content"])


class AnthropicProvider(LLMProvider):
    name = "anthropic"

    def __init__(self, s: Settings):
        self.s = s

    async def generate(self, system: str, user: str, ctx: ProviderContext) -> LLMResult:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post("https://api.anthropic.com/v1/messages",
                             headers={"x-api-key": self.s.anthropic_api_key,
                                      "anthropic-version": "2023-06-01"},
                             json={"model": self.s.anthropic_model, "max_tokens": 700, "temperature": 0.2,
                                   "system": system, "messages": [{"role": "user", "content": user}]})
            r.raise_for_status()
            text = "".join(b.get("text", "") for b in r.json().get("content", []) if b.get("type") == "text")
            return _parse_json_result(text)


class OpenRouterProvider(LLMProvider):
    """OpenAI-compatible chat completions through OpenRouter (any model id, e.g. openai/gpt-4o-mini)."""
    name = "openrouter"

    def __init__(self, s: Settings):
        self.s = s

    async def generate(self, system: str, user: str, ctx: ProviderContext) -> LLMResult:
        async with httpx.AsyncClient(timeout=40) as c:
            r = await c.post("https://openrouter.ai/api/v1/chat/completions",
                             headers={"Authorization": f"Bearer {self.s.openrouter_api_key}",
                                      "HTTP-Referer": self.s.public_base_url, "X-Title": "OmniQMS"},
                             json={"model": self.s.openrouter_model, "temperature": 0.2, "max_tokens": 700,
                                   "response_format": {"type": "json_object"},
                                   "messages": [{"role": "system", "content": system},
                                                {"role": "user", "content": user}]})
            r.raise_for_status()
            return _parse_json_result(r.json()["choices"][0]["message"]["content"] or "")


def get_provider(s: Settings | None = None) -> LLMProvider:
    s = s or get_settings()
    if s.llm_provider == "openrouter" and s.openrouter_api_key:
        return OpenRouterProvider(s)
    if s.llm_provider == "openai" and s.openai_api_key:
        return OpenAIProvider(s)
    if s.llm_provider == "anthropic" and s.anthropic_api_key:
        return AnthropicProvider(s)
    return MockProvider()
