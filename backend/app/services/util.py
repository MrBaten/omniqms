"""Small shared helpers (time handling, workspace settings)."""
from datetime import datetime, timezone

from app.models import DEFAULT_SETTINGS, Workspace


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def as_utc(dt: datetime | None) -> datetime | None:
    """SQLite returns naive datetimes; everything we store is UTC."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def ws_settings(ws: Workspace) -> dict:
    merged = dict(DEFAULT_SETTINGS)
    merged.update(ws.settings or {})
    return merged
