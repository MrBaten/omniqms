"""FastAPI application factory."""
from __future__ import annotations

import asyncio
import contextlib
import logging
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response

from app.core.config import get_settings
from app.core.db import create_all
from app.core.security import csrf_ok, ensure_safe_config

log = logging.getLogger("omniqms")


class _RedactQuery(logging.Filter):
    """Keep OAuth codes, states and verify tokens out of access logs."""
    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple) and len(record.args) >= 3 and isinstance(record.args[2], str):
            a = list(record.args)
            a[2] = a[2].split("?", 1)[0] + ("?…" if "?" in a[2] else "")
            record.args = tuple(a)
        return True


logging.getLogger("uvicorn.access").addFilter(_RedactQuery())
if not log.handlers:  # app-level INFO logs (webhooks received, replies sent) — never message text or tokens
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    log.addHandler(_h)
    log.setLevel(logging.INFO)
    log.propagate = False

WEB_DIST = Path(__file__).resolve().parents[2] / "web" / "dist"

CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: https:; "
       "font-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'; "
       "object-src 'none'")


MAX_API_BODY = 2_000_000  # KB docs are ≤200k chars (≤~800 KB UTF-8); nothing legitimate is bigger


def _body_too_large(request: Request) -> Response | None:
    if not request.url.path.startswith("/api/") or request.method not in ("POST", "PUT", "PATCH", "DELETE"):
        return None
    cl = request.headers.get("content-length")
    if cl is None:
        if "chunked" in (request.headers.get("transfer-encoding") or "").lower():
            return JSONResponse({"detail": "Content-Length required"}, status_code=411)
        return None
    if not cl.strip().isdigit() or int(cl) > MAX_API_BODY:
        return JSONResponse({"detail": "Payload too large"}, status_code=413)
    return None


def create_app() -> FastAPI:
    settings = get_settings()
    ensure_safe_config(settings)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        create_all()
        stop = asyncio.Event()
        task = None
        if settings.worker_enabled and settings.env != "test":
            from app.worker.runner import run_worker
            task = asyncio.create_task(run_worker(stop))
        yield
        stop.set()
        if task:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(task, timeout=10)

    app = FastAPI(title="OmniQMS", version="0.1.0", lifespan=lifespan,
                  docs_url=None if settings.is_prod else "/docs", redoc_url=None,
                  openapi_url=None if settings.is_prod else "/openapi.json")

    origins = [o.strip() for o in settings.cors_origins.split(",") if o.strip()]
    if origins:
        app.add_middleware(CORSMiddleware, allow_origins=origins, allow_credentials=True,
                           allow_methods=["GET", "POST", "PUT", "DELETE"],
                           allow_headers=["Content-Type", "X-CSRF-Token"])

    @app.middleware("http")
    async def security_middleware(request: Request, call_next):
        too_large = _body_too_large(request)
        if too_large is not None:
            resp: Response = too_large
        elif not csrf_ok(request):
            resp = JSONResponse({"detail": "CSRF token missing or invalid"}, status_code=403)
        else:
            resp = await call_next(request)
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        resp.headers.setdefault("Content-Security-Policy", CSP)
        resp.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        if settings.cookie_secure:
            resp.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        if request.url.path.startswith("/api/"):
            resp.headers.setdefault("Cache-Control", "no-store")
        return resp

    from app.api import auth, connections, conversations, legal, webhooks, workspace
    for r in (auth.router, connections.router, conversations.router, workspace.router, webhooks.router, legal.router):
        app.include_router(r)

    @app.get("/healthz", include_in_schema=False)
    def healthz():
        return {"ok": True}

    @app.get("/", include_in_schema=False)
    def root():
        return RedirectResponse("/app/")

    @app.get("/app", include_in_schema=False)
    @app.get("/app/{path:path}", include_in_schema=False)
    def spa(path: str = ""):
        dist = WEB_DIST.resolve()
        index = dist / "index.html"
        if not index.exists():
            return JSONResponse({"detail": "Dashboard not built. Run `npm run build` in web/."}, status_code=404)
        if path:
            candidate = (dist / path).resolve()
            if candidate.is_file() and candidate.is_relative_to(dist):
                return FileResponse(candidate)
        return FileResponse(index, headers={"Cache-Control": "no-cache"})

    @app.exception_handler(RequestValidationError)
    async def friendly_validation(request: Request, exc: RequestValidationError):
        """Turn pydantic errors into one readable sentence for the dashboard (never echoes input values)."""
        labels = {"email": "Email", "password": "Password", "workspace_name": "Business / workspace name",
                  "name": "Name", "text": "Message", "title": "Title", "content": "Content", "question": "Question"}
        msgs = []
        for err in exc.errors():
            field = next((str(p) for p in reversed(err.get("loc", ())) if isinstance(p, str) and p != "body"), "")
            label = labels.get(field, field.replace("_", " ").capitalize() or "Request")
            typ, ctx = err.get("type", ""), err.get("ctx") or {}
            if field == "email":
                msgs.append("Email: enter a valid address like name@example.com")
            elif typ == "string_too_short":
                msgs.append(f"{label}: must be at least {ctx.get('min_length')} characters")
            elif typ == "string_too_long":
                msgs.append(f"{label}: must be at most {ctx.get('max_length')} characters")
            elif typ == "missing":
                msgs.append(f"{label}: required")
            else:
                msgs.append(f"{label}: {str(err.get('msg', 'invalid')).removeprefix('Value error, ')}")
        return JSONResponse({"detail": "; ".join(dict.fromkeys(msgs)) or "Invalid request"}, status_code=422)

    return app


app = create_app()
