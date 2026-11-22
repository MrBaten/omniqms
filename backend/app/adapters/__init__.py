"""Adapter registry. Core resolves platforms only through get_adapter / all_adapters."""
from __future__ import annotations

from app.adapters.base import PlatformAdapter
from app.adapters.facebook import FacebookAdapter
from app.adapters.instagram import InstagramAdapter
from app.adapters.sandbox import SandboxAdapter
from app.adapters.youtube import YouTubeAdapter
from app.core.config import get_settings

_REGISTRY: dict[str, PlatformAdapter] = {
    "facebook": FacebookAdapter(),
    "instagram": InstagramAdapter(),
    "youtube": YouTubeAdapter(),
    "sandbox": SandboxAdapter(),
}


def get_adapter(platform: str) -> PlatformAdapter:
    """Return the adapter for a platform. Raises KeyError for unknown (or disabled sandbox)."""
    if platform == "sandbox" and not get_settings().sandbox_enabled:
        raise KeyError(platform)
    return _REGISTRY[platform]


def all_adapters() -> list[PlatformAdapter]:
    out = [_REGISTRY["facebook"], _REGISTRY["instagram"], _REGISTRY["youtube"]]
    if get_settings().sandbox_enabled:
        out.append(_REGISTRY["sandbox"])
    return out


__all__ = ["get_adapter", "all_adapters", "PlatformAdapter"]
