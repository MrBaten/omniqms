# OmniQMS — Multi-platform Social Query Management (build plan)

Product: a multi-tenant SaaS where a business connects Facebook Pages, Instagram professional
accounts and YouTube channels with one "Connect" button each. Incoming comments and DMs land in
one inbox, an AI engine answers them from the workspace's knowledge base, and low-confidence or
sensitive items go to a human agent.

## Stack
- Backend: Python 3.11+, FastAPI, SQLAlchemy 2.x (SQLite in dev, Postgres in prod), httpx.
- Queue: DB-backed job table + async worker loop (no Redis needed; swappable later).
- AI: pluggable LLM provider (`mock` offline default, `openai`, `anthropic`) + pure-Python BM25
  retrieval (optional OpenAI embeddings hybrid).
- Dashboard: React + Vite + TypeScript in `web/`, built to `web/dist`, served by FastAPI at `/app`.
- Tests: pytest + respx (all platform HTTP mocked).

## Directory ownership (parallel build — do not edit outside your area)
| Area | Owner | Paths |
|---|---|---|
| Shared contracts (frozen) | planner | `backend/app/models.py`, `backend/app/core/{config,db,crypto}.py`, `backend/app/adapters/base.py`, this file |
| Backend core | Agent A | `backend/app/main.py`, `backend/app/api/**`, `backend/app/services/**`, `backend/app/ai/**`, `backend/app/worker/**`, `backend/app/core/security.py`, `backend/tests/core/**` |
| Platform connectors | Agent B | `backend/app/adapters/**` (except base.py), `backend/tests/adapters/**` |
| Dashboard | Agent C | `web/**` |

If a contract needs to change, note it in `CONTRACT_REQUESTS.md` instead of editing it.

## Data flow
1. Connect: dashboard → `POST /api/w/{wid}/connections/{platform}/start` → backend stores a single-use
   `OAuthState` and returns `authorize_url` → user approves on the platform → platform redirects to
   `GET /oauth/callback/{platform}?code&state` → backend validates state, calls
   `adapter.exchange_code()` (may return several accounts, e.g. several Pages), encrypts tokens,
   upserts `ConnectedAccount`, calls `adapter.on_connected()` (webhook subscription) → redirects to
   `/app/connections?connected={platform}`.
2. Ingest: Meta → `POST /webhooks/{platform}` → `adapter.verify_webhook_signature()` (reject 403 if
   bad) → `adapter.parse_webhook()` → `services.ingest.ingest_event()` per event. YouTube: worker
   calls `adapter.poll()` every `YOUTUBE_POLL_SECONDS` per account → same `ingest_event()`.
   `ingest_event`: find account by (platform, account_external_id) → drop own-echo (author ==
   account) → dedupe by (account_id, external_message_id) → upsert Contact + Conversation →
   insert inbound Message → enqueue job `generate_reply`.
3. Answer: worker `generate_reply` → if workspace auto-reply off: leave `open` → else
   `ai.engine.answer()` → if confidence ≥ threshold and not needs_human: create outbound AI message
   and enqueue `send_reply`; else conversation `pending_human` and store AI draft
   (`delivery_status="draft"`).
4. Send: worker `send_reply` → `adapter.send_reply(AccountContext, ReplyTarget, text)` → update
   message (`sent`/`failed`), on `needs_reconnect` set account status `needs_reconnect`; retry
   retryable errors with backoff (max 5 attempts).

## Platform rules encoded in adapters
- Messenger / Instagram DM: AI or human may reply within 24h of the customer's last message
  (`messaging_type=RESPONSE`). From 24h to 7 days only a human may reply, using
  `messaging_type=MESSAGE_TAG, tag=HUMAN_AGENT`. After 7 days → `SendResult(ok=False,
  error="outside_messaging_window")`.
- Comments: reply in thread (FB `POST /{comment-id}/comments`, IG `POST /{comment-id}/replies`,
  YouTube `comments.insert` with `parentId`).
- YouTube: no DMs; no webhooks → polling `commentThreads.list?allThreadsRelatedToChannelId=`.
  Quota: comments.insert costs 50 units of a 10,000/day default.
- A `sandbox` adapter simulates a platform so the full pipeline works without real credentials.

## REST API (JSON; consumed by the dashboard)
Auth: session JWT in httpOnly cookie `qms_session`; CSRF double-submit cookie `qms_csrf` must be
echoed in header `X-CSRF-Token` on every POST/PUT/PATCH/DELETE under `/api` (except login/register).
All `/api/w/{wid}/...` routes require membership of workspace `wid` (404 if not a member).

- `POST /api/auth/register` `{email, password, name, workspace_name}` → `{user, workspaces}`
- `POST /api/auth/login` `{email, password}` → `{user, workspaces}`
- `POST /api/auth/logout`
- `GET  /api/auth/me` → `{user:{id,email,name}, workspaces:[{id,name,role}]}`
- `GET  /api/w/{wid}/platforms` → `[{platform, label, configured: bool, supports_dm, supports_comments}]`
- `GET  /api/w/{wid}/connections` → `[{id, platform, external_id, name, status, last_event_at, created_at}]`
- `POST /api/w/{wid}/connections/{platform}/start` → `{authorize_url}`
- `DELETE /api/w/{wid}/connections/{id}`
- `GET  /oauth/callback/{platform}?code=&state=` → 302 `/app/connections?connected={platform}` or `?error=...`
- `GET  /api/w/{wid}/conversations?status=&platform=&kind=&q=&limit=&offset=` →
  `{items:[{id, platform, kind, status, contact:{display_name}, account_name, last_message_preview,
  last_customer_message_at, updated_at}], total}`
- `GET  /api/w/{wid}/conversations/{cid}` → `{conversation…, messages:[{id, direction, author, text,
  ai_confidence, ai_sources, delivery_status, error, created_at}], reply_window:{dm_ai_allowed,
  dm_human_allowed, expires_at}}`
- `POST /api/w/{wid}/conversations/{cid}/reply` `{text}` → message (human reply, queued)
- `POST /api/w/{wid}/conversations/{cid}/drafts/{mid}/approve` `{text?}` → message (send AI draft, optionally edited)
- `POST /api/w/{wid}/conversations/{cid}/status` `{status: open|pending_human|resolved}`
- `GET/POST /api/w/{wid}/knowledge` ; `PUT/DELETE /api/w/{wid}/knowledge/{doc_id}` —
  doc `{id, title, source_type, content, created_at, updated_at}` (content ≤ 200k chars)
- `POST /api/w/{wid}/knowledge/test` `{question}` → `{answer, confidence, needs_human, sources:[{doc_id,title,snippet}]}`
- `GET/PUT /api/w/{wid}/settings` → `{auto_reply_enabled, confidence_threshold, brand_voice,
  reply_language, escalation_keywords:[...], business_hours_note}`
- `GET  /api/w/{wid}/stats` → `{open, pending_human, resolved, auto_replied_24h, by_platform:{...}}`
- `POST /api/w/{wid}/sandbox/simulate` `{kind: dm|comment, text, author_name}` (only if `SANDBOX_ENABLED`)
- `GET  /webhooks/{platform}` (Meta verify handshake) ; `POST /webhooks/{platform}`
- `GET  /healthz`

## Security requirements (all agents)
- Platform tokens encrypted at rest (`core.crypto`), never returned by the API, never logged.
- Webhooks: HMAC-SHA256 `X-Hub-Signature-256` with the app secret, constant-time compare, body
  size limit 1 MB.
- OAuth `state`: random 32 bytes, single-use, 10-minute expiry, bound to user + workspace + platform.
- Passwords bcrypt; login rate-limited; generic auth errors.
- Every DB query scoped by `workspace_id`; IDs from URLs never trusted without membership check.
- Prompt injection: customer text is untrusted data inside delimiters; AI may only answer from KB;
  output length capped; escalation keywords force human.
- Security headers + CSP on dashboard; CORS closed by default.

## Test plan
Unit: adapters (OAuth URLs, code exchange, webhook parsing, signatures, send rules, window rules,
YouTube polling) with respx; AI retrieval + engine with mock provider; auth, CSRF, tenant isolation.
End-to-end: register → add KB → sandbox connect → simulate DM/comment → auto-reply sent; low
confidence → pending_human → human reply; signed Meta webhook → reply via mocked Graph API.
Security: independent review + regression tests for each finding.

## Adapter registry (Agent B provides, Agent A consumes)
`backend/app/adapters/__init__.py` exports:
- `get_adapter(platform: str) -> PlatformAdapter` (raises `KeyError` for unknown platform)
- `all_adapters() -> list[PlatformAdapter]` (facebook, instagram, youtube, and sandbox when `sandbox_enabled`)
Adapters read credentials from `get_settings()` at call time (tests override env and clear the cache).
Sandbox: `SandboxAdapter.make_event(account_external_id, kind, text, author_name) -> InboundEvent`;
OAuth for sandbox is simulated: `authorize_url` returns `{public_base_url}/oauth/callback/sandbox?code=sandbox-ok&state=...`;
`exchange_code` returns one grant `external_id="sandbox-<random>"`; `send_reply` always succeeds and
records sent messages in an in-memory list `SandboxAdapter.sent` for tests.
