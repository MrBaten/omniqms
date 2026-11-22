
## From Agent B (adapters)
1. `SendResult` has no `tokens` field. YouTube `send_reply` refreshes an expired access token itself but cannot hand the new token back, so core keeps the stale one until the next `poll()` (which does return `PollResult.tokens`). Suggest adding `tokens: TokenBundle | None = None` to `SendResult`; until then core should call `adapter.refresh_tokens(ctx)` before `send_reply` and persist the result (works for youtube + instagram; returns None when no refresh is needed).
2. Instagram long-lived tokens (~60 days) need periodic `refresh_tokens()`; core should call it (e.g. daily job) for instagram accounts and persist the returned TokenBundle. On `AdapterError(needs_reconnect=True)` mark the account `needs_reconnect`.
