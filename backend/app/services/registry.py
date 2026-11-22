"""Indirection over the adapter registry in app.adapters (owned by the connectors module).

Tests install fakes with `set_override([...])`; production code always goes through these helpers so
the registry is imported lazily.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.adapters.base import PlatformAdapter

_override: dict[str, "PlatformAdapter"] | None = None


def set_override(adapters: list["PlatformAdapter"] | None) -> None:
    global _override
    _override = None if adapters is None else {a.platform: a for a in adapters}


def get_adapter(platform: str) -> "PlatformAdapter":
    if _override is not None:
        return _override[platform]  # KeyError for unknown
    from app import adapters
    return adapters.get_adapter(platform)


def all_adapters() -> list["PlatformAdapter"]:
    if _override is not None:
        return list(_override.values())
    from app import adapters
    return adapters.all_adapters()


def find_adapter(platform: str) -> "PlatformAdapter | None":
    try:
        return get_adapter(platform)
    except (KeyError, AttributeError, ImportError):
        return None
