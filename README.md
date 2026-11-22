# OmniQMS — Social Media Query Management

One inbox for Facebook Page comments + Messenger, Instagram comments + DMs, and YouTube comments.
Businesses connect each account with one **Connect** button (OAuth). An AI answers from the
workspace's knowledge base; low-confidence or sensitive messages go to a human with an AI draft.

## What's included
| Part | Path |
|---|---|
| API + worker (FastAPI) | `backend/app` |
| Platform connectors | `backend/app/adapters` — `facebook.py`, `instagram.py`, `youtube.py`, `sandbox.py` |
| AI engine (BM25 retrieval + LLM) | `backend/app/ai` — providers: `mock` (offline), `openai`, `anthropic` |
| Dashboard (React) | `web/` → served at `/app` |
| Tests | `backend/tests` — core, adapters, end-to-end, security |

## Run locally in 2 minutes (sandbox mode, no Meta/Google keys needed)
```bash
cd web && npm install && npm run build && cd ..
cd backend && pip install -r requirements.txt
uvicorn app.main:app --reload
```
Open http://localhost:8000/app → Register → Knowledge Base (add your FAQ) → Connections → **Connect Sandbox**
→ Inbox → simulate a DM/comment and watch the AI reply.

Run tests: `cd backend && python3 -m pytest -q`

## Production deploy (Docker)
```bash
cp .env.example .env    # fill in every value (see comments in the file)
docker compose up -d --build
```
Put it behind HTTPS (Caddy/Nginx/your host's TLS). `PUBLIC_BASE_URL` must be the public HTTPS URL.
The app refuses to start with `ENV=prod` if `SECRET_KEY`, `TOKEN_ENCRYPTION_KEY` or `COOKIE_SECURE` are unsafe.

## Connect the real platforms
Your OAuth redirect URIs and webhook URLs (replace the domain):

| Platform | OAuth redirect URI | Webhook URL |
|---|---|---|
| Facebook | `https://YOUR-DOMAIN/oauth/callback/facebook` | `https://YOUR-DOMAIN/webhooks/facebook` (fields: `messages`, `feed`) |
| Instagram | `https://YOUR-DOMAIN/oauth/callback/instagram` | `https://YOUR-DOMAIN/webhooks/instagram` (fields: `comments`, `messages`) |
| YouTube | `https://YOUR-DOMAIN/oauth/callback/youtube` | none — comments are polled every `YOUTUBE_POLL_SECONDS` |

**Meta (Facebook + Instagram)** — developers.facebook.com → create a Business app.
1. Add *Facebook Login for Business* and *Messenger*; add *Instagram → API setup with Instagram login*.
2. Set the redirect URIs above; set webhook callback URLs + verify tokens (`META_WEBHOOK_VERIFY_TOKEN`, `IG_WEBHOOK_VERIFY_TOKEN`).
3. Permissions requested by the app: Facebook `pages_show_list, pages_messaging, pages_manage_metadata,
   pages_read_engagement, pages_manage_engagement, pages_read_user_content, business_management`;
   Instagram `instagram_business_basic, instagram_business_manage_messages, instagram_business_manage_comments`.
4. Until **App Review + Advanced Access** (and business verification) are approved, only people with a role on
   your app can connect. Plan for this before selling to other companies.

**Google (YouTube)** — console.cloud.google.com → enable *YouTube Data API v3* → OAuth client (Web).
Scope `youtube.force-ssl` is sensitive: Google app verification is needed before public use.
Default quota is 10,000 units/day; each reply costs 50 (≈200 replies/day/project) — request more quota if needed.

**AI** — set `LLM_PROVIDER=openai` (or `anthropic`) and the API key. `mock` works offline (quotes the best KB match).

## Platform rules enforced
- DMs: AI/human may reply within 24h of the customer's last message; 24h–7d only a human (sent with the
  `HUMAN_AGENT` tag); after 7 days replies are blocked until the customer writes again.
- Instagram long-lived tokens are refreshed automatically before they expire (~60 days).
- An account (Page/IG/channel) can belong to only one workspace at a time.
- Escalation keywords (refund, complaint, …) always go to a human.

## Known limits
- Adapters are tested against mocked APIs only; do a live test with your own Page before launch, and check
  `META_GRAPH_VERSION` against Meta's current version.
- One worker process (DB-backed queue). For high volume, run Postgres and move the queue to Redis.
- Login rate limiting is per process (use a shared store if you run several instances).
