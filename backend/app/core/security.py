"""Authentication, sessions, CSRF, membership checks and login rate limiting."""
from __future__ import annotations

import threading
import time
from collections import deque
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import Depends, HTTPException, Request, Response
from passlib.context import CryptContext
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.crypto import constant_time_equals, random_token
from app.core.db import get_db
from app.models import Membership, RevokedSession, User, Workspace

SESSION_COOKIE = "qms_session"
CSRF_COOKIE = "qms_csrf"
CSRF_HEADER = "X-CSRF-Token"
DEFAULT_SECRET = "dev-insecure-secret-change-me"
CSRF_EXEMPT = {"/api/auth/login", "/api/auth/register"}
UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

_pwd = CryptContext(schemes=["bcrypt"], deprecated="auto")
_DUMMY_HASH = _pwd.hash("dummy-password-for-timing")


# --- startup safety --------------------------------------------------------------------------
def ensure_safe_config(s: Settings) -> None:
    if s.env not in ("dev", "test", "prod"):  # e.g. ENV=production would silently skip every prod check
        raise RuntimeError(f"Unsafe configuration: ENV must be dev, test or prod (got {s.env!r})")
    if "*" in [o.strip() for o in s.cors_origins.split(",")]:  # credentialed CORS would reflect any origin
        raise RuntimeError("Unsafe configuration: CORS_ORIGINS must list explicit origins, not '*'")
    if not s.is_prod:
        return
    problems = []
    if s.secret_key == DEFAULT_SECRET or len(s.secret_key) < 32:
        problems.append("SECRET_KEY must be set to a random value of at least 32 characters")
    if not s.token_encryption_key:
        problems.append("TOKEN_ENCRYPTION_KEY must be set")
    if not s.cookie_secure:
        problems.append("COOKIE_SECURE must be true in production")
    if problems:
        raise RuntimeError("Unsafe production configuration: " + "; ".join(problems))


# --- passwords -------------------------------------------------------------------------------
def hash_password(password: str) -> str:
    return _pwd.hash(password)


def verify_password(password: str, password_hash: str | None) -> bool:
    try:
        if not password_hash:
            _pwd.verify(password, _DUMMY_HASH)  # equalise timing
            return False
        return _pwd.verify(password, password_hash)
    except (ValueError, TypeError):
        return False


# --- sessions --------------------------------------------------------------------------------
def create_session_token(user_id: int, s: Settings | None = None) -> str:
    s = s or get_settings()
    now = datetime.now(timezone.utc)
    payload = {"sub": str(user_id), "typ": "session", "iat": now, "jti": random_token(16),
               "exp": now + timedelta(hours=s.session_hours)}
    return jwt.encode(payload, s.secret_key, algorithm="HS256")


def decode_session_claims(token: str, s: Settings | None = None) -> dict | None:
    """Verified session claims (sub, jti, exp) or None."""
    s = s or get_settings()
    try:
        payload = jwt.decode(token, s.secret_key, algorithms=["HS256"],
                             options={"require": ["exp", "sub", "jti"]})
    except jwt.PyJWTError:
        return None
    if payload.get("typ") != "session" or not isinstance(payload.get("jti"), str):
        return None
    try:
        payload["uid"] = int(payload["sub"])
    except (KeyError, ValueError):
        return None
    return payload


def decode_session_token(token: str, s: Settings | None = None) -> int | None:
    claims = decode_session_claims(token, s)
    return claims["uid"] if claims else None


def revoke_session(request: Request, db: Session) -> None:
    """Server-side invalidation of the presented session token (logout)."""
    claims = decode_session_claims(request.cookies.get(SESSION_COOKIE) or "")
    if not claims:
        return
    now = datetime.now(timezone.utc)
    db.execute(delete(RevokedSession).where(RevokedSession.expires_at < now))
    if db.get(RevokedSession, claims["jti"][:64]) is None:
        db.add(RevokedSession(jti=claims["jti"][:64],
                              expires_at=datetime.fromtimestamp(claims["exp"], tz=timezone.utc)))
    db.commit()


def issue_session(response: Response, user_id: int) -> None:
    s = get_settings()
    max_age = s.session_hours * 3600
    response.set_cookie(SESSION_COOKIE, create_session_token(user_id, s), max_age=max_age, httponly=True,
                        secure=s.cookie_secure, samesite="lax", path="/")
    response.set_cookie(CSRF_COOKIE, random_token(24), max_age=max_age, httponly=False,
                        secure=s.cookie_secure, samesite="lax", path="/")


def clear_session(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/")
    response.delete_cookie(CSRF_COOKIE, path="/")


def csrf_ok(request: Request) -> bool:
    """Double-submit check for unsafe /api requests."""
    if request.method not in UNSAFE_METHODS:
        return True
    path = request.url.path
    if not path.startswith("/api/") or path in CSRF_EXEMPT:
        return True
    cookie = request.cookies.get(CSRF_COOKIE) or ""
    header = request.headers.get(CSRF_HEADER) or ""
    return bool(cookie) and bool(header) and constant_time_equals(cookie, header)


def user_from_request(request: Request, db: Session) -> User | None:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    claims = decode_session_claims(token)
    if claims is None or db.get(RevokedSession, claims["jti"][:64]) is not None:
        return None
    user = db.get(User, claims["uid"])
    if user is None or not user.is_active:
        return None
    return user


def current_user(request: Request, db: Session = Depends(get_db)) -> User:
    user = user_from_request(request, db)
    if user is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user


class MemberContext:
    def __init__(self, user: User, workspace: Workspace, membership: Membership):
        self.user = user
        self.workspace = workspace
        self.membership = membership

    def __iter__(self):  # allows `user, ws, m = ctx`
        return iter((self.user, self.workspace, self.membership))

    def require_role(self, *roles: str) -> None:
        if self.membership.role not in roles:
            raise HTTPException(status_code=403, detail="Insufficient role")


def workspace_member(wid: int, user: User = Depends(current_user), db: Session = Depends(get_db)) -> MemberContext:
    m = db.scalar(select(Membership).where(Membership.user_id == user.id, Membership.workspace_id == wid))
    if m is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    ws = db.get(Workspace, wid)
    if ws is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    return MemberContext(user, ws, m)


ADMIN_ROLES = ("owner", "admin")


# --- login rate limiter ----------------------------------------------------------------------
class RateLimiter:
    def __init__(self, limit: int = 10, window_seconds: int = 300):
        self.limit = limit
        self.window = window_seconds
        self._hits: dict[str, deque] = {}
        self._lock = threading.Lock()

    def _prune(self, key: str, now: float) -> deque:
        q = self._hits.setdefault(key, deque())
        while q and now - q[0] > self.window:
            q.popleft()
        return q

    def blocked(self, key: str) -> bool:
        with self._lock:
            return len(self._prune(key, time.monotonic())) >= self.limit

    def hit(self, key: str) -> None:
        with self._lock:
            now = time.monotonic()
            self._prune(key, now).append(now)
            if len(self._hits) > 50_000:  # bound memory
                for k in list(self._hits)[:10_000]:
                    self._hits.pop(k, None)

    def reset(self, key: str | None = None) -> None:
        with self._lock:
            if key is None:
                self._hits.clear()
            else:
                self._hits.pop(key, None)


login_limiter = RateLimiter(limit=10, window_seconds=300)
login_ip_limiter = RateLimiter(limit=50, window_seconds=300)
register_ip_limiter = RateLimiter(limit=20, window_seconds=3600)  # slows account/email enumeration + signup spam


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"
