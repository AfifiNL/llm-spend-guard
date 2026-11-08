# llm-spend-guard

A budget guard for LLM calls, in Python on PostgreSQL. Before each call it reserves the call's
worst-case cost against a daily global budget and a daily per-tenant budget; after the call it
settles the actual cost from the provider's reported usage. A call that could take a budget past
its limit is refused before it is made, and so is a call to a model without a checked price.

- **Reserve, then settle**, in transactions that lock the day's counters: concurrent calls can
  never both fit into the same remaining budget.
- **No checked price, no call**: every price is a dated, sourced row in the budgets' currency.
  No price, or only one older than 31 days, refuses the call; a price dated in the future is
  not used until its date.
- **Alerts** once per scope and day when settled spend reaches 80% (configurable), and a
  **pause** alert when a call is refused or settled spend reaches 100%.
- **Append-only ledger and price book** (triggers reject edits), running totals for fast
  checks, and a `reconcile` that recomputes the totals from the ledger.
- **OpenTelemetry spans** per call with the GenAI attributes and the reserved and charged
  amounts, for any exporter.
- **API keys bound to tenants** (only their SHA-256 is stored), so one client cannot spend
  another client's budget; a shared token remains for a single trusted caller.
- **Idempotent requests**: a client `request_id` (per tenant) is charged once; a retry gets the
  stored result back with `charged_now: 0`, a concurrent retry gets 409 `request_in_flight`.
- **Providers**: a mock, so the demo and the tests spend nothing; a Gemini adapter with a
  recorded live probe that CI replays through a real ledger; OpenAI and Anthropic adapters that
  are tested with fake clients only and **have never been called against the real APIs**.

## What it does

`make demo` (offline, mock model, a throwaway local PostgreSQL):

```text
== price: mock-small 2.00 in / 8.00 out per 1M tokens; budget per tenant 0.005/day
== team-214326 calls the model (max 60 output tokens each)
   call  1: reserved 0.000786  charged 0.000296
   ...
   call 14: reserved 0.000786  charged 0.000296
           ALERT warn on tenant:team-214326: 0.004144 of 0.005000
   call 15: reserved 0.000786  charged 0.000296
   call 16: REFUSED before the call: tenant:team-214326: 0.004440 spent or reserved + 0.000786 worst case for this call > 0.005000 daily limit
           ALERT pause on tenant:team-214326: 0.004440 of 0.005000
== a model without a checked price
   REFUSED: no checked price for model 'mock-large': add one to the price book
== status
   global: settled 0.004440, reserved 0.000000, remaining 0.045560 of 0.050000
   tenant:team-214326: settled 0.004440, reserved 0.000000, remaining 0.000560 of 0.005000
== trace spans (any OpenTelemetry exporter can ship these)
   llm.complete: outcome=ok reserved=786 cost=296 model=mock-small
   llm.complete: outcome=refused_budget reserved=None cost=None model=mock-small
   llm.complete: outcome=refused_no_price reserved=None cost=None model=mock-large
== reconcile: ledger and counters agree
```

Call 16 shows the trade-off this design makes on purpose: the tenant had spent 0.004440 of
0.005, but the call's worst case (0.000786) no longer fit, so it was refused. A guarantee that
the limit holds costs some headroom; the larger `max_output_tokens`, the more.

## Quick start

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/). No database setup: without
`SPEND_GUARD_DATABASE_URL`, a pip-packaged PostgreSQL 16 is started in a temporary directory.
With your own database, PostgreSQL 14 or newer; the demo and the probe then work in a scratch
schema that they drop afterwards, so they never touch real budgets, prices or ledger rows.

```bash
make install    # .venv with extras, dev tools and the local PostgreSQL (locked)
make demo       # the run above
make check      # lint, strict types, 119 tests on PostgreSQL, probe replay, demo (what CI runs)
```

With your own PostgreSQL:

```bash
export SPEND_GUARD_DATABASE_URL=postgresql://user@localhost:5432/spend
uv run llm-spend-guard price-add my-model --input 0.75 --output 3.75 \
    --checked-at 2026-11-08T21:25Z --source "vendor pricing page"
uv run llm-spend-guard budget-set global 5.00
uv run llm-spend-guard budget-set '*' 0.50        # default per tenant
uv run llm-spend-guard budget-set team-a 2.00     # override for one tenant
SPEND_GUARD_API_TOKEN=$(openssl rand -hex 16) uv run llm-spend-guard serve
```

## How it works

```text
complete(tenant, model, prompt, max_output_tokens)
   │
   ▼
price ──── newest row for the model, checked within 31 days and not in the future ── else refuse
   │
   ▼
reserve ── worst case = price × (input bound, max_output_tokens); one transaction:
   │       lock today's counters (global, tenant) in a fixed order → check every limit →
   │       insert reservation + ledger row → add to "reserved" → alert at 80% once
   │       a limit would be exceeded → record "pause", refuse (the provider is never called)
   ▼
provider call ──── rejected before work (400/401/403/404/429) → release: charge nothing
   │          ──── failed with unknown usage (timeout, 5xx)  → charge the full reservation
   ▼
settle ─── actual = price × reported usage, rounded up to the micro-unit;
           reserved → settled in one transaction; actual > reserved is recorded as an overrun
```

- **Input bound.** The guard does not tokenize. A tokenizer that covers at least one byte per
  token cannot produce more tokens than the text has UTF-8 bytes, so the bound is the byte
  count plus 64 tokens for chat formatting. It over-reserves input (cheap) to never
  under-reserve. The live probe below checks it.
- **Output bound.** `max_output_tokens` is passed to the provider and reserved in full.
  Reasoning ("thinking") tokens are billed as output and counted as output.
- **Money** is integer micro-units of the price book's currency; costs round up.
- **Fail closed.** No global budget, or no budget for the tenant and no default: refused.
- **Abandoned reservations** (a caller crashed between reserve and settle) are charged at their
  worst case by `llm-spend-guard sweep`. If the slow call then settles after all, its real usage
  is recorded and anything above the worst case is charged as an overrun; nothing is refunded.

### HTTP API

| Method | Path | Result |
|---|---|---|
| `POST` | `/v1/complete` | `{tenant?, model, prompt, system?, max_output_tokens, request_id?}` → text, usage, reserved, `cost`, `charged_now`, `replayed`; errors below |
| `GET` | `/v1/budgets/{tenant}` | today's limit, reserved, settled and remaining per scope |
| `GET` | `/healthz` | liveness |

Both `/v1` routes take `Authorization: Bearer <credential>`, one of:

- **an API key** from `llm-spend-guard key-create <tenant>` (printed once; only its SHA-256 is
  stored). The key decides the tenant: the body may leave `tenant` out, and a different tenant
  in the body or the path is refused (403). `key-list` shows key ids, `key-revoke <id>` ends one.
- **the shared token** `SPEND_GUARD_API_TOKEN`, for one trusted caller that names the tenant in
  every request and may use any. Unset it once every client has its own key.

A refused credential gets 401 `unauthorized`, whether or not keys exist. Only when there is no
shared token and no active key (checked at most every 10 seconds) is the answer 503
`api_disabled`: nothing could pass. Prices, budgets
and keys can only be changed with the CLI, not over HTTP. A 402 names the scope that is
exhausted, never another scope's amounts.

### Idempotent requests

A client that may retry (its own timeout, a redelivered message) sends a `request_id` (UUID).
Ids are per tenant: another tenant's identical id is a different request and reveals nothing.

| what happened to that id before | what a new request with it gets |
|---|---|
| nothing | normal processing; the id is claimed |
| still in flight | **409** `request_in_flight` with `Retry-After: 5` |
| completed | the stored result, `"replayed": true`, `"charged_now": "0.000000"`; no new reservation, no model call |
| refused before any call (price, budget) or rejected by the provider before any work | normal processing: nothing was charged, so the id was freed |
| failed in a way that may have been billed, or abandoned (in flight over 15 minutes) | **409** `request_id_spent`: retry with a **new** id; an unknown outcome is never charged twice |
| used for a different request (model, prompt, system or `max_output_tokens` differ) | **409** `request_id_conflict` |

Every response carries `cost` (what the request cost) and `charged_now` (what this HTTP call
added to the ledger: zero for a replay). Sum `charged_now`, not `cost`. If the result of a
completed call cannot be stored, the caller still gets it; the id then answers
`request_in_flight` and, after 15 minutes, `request_id_spent`.

`llm-spend-guard idempotency-prune` marks abandoned requests as failed, forgets completed ids
after 24 hours and failed ones after 30 days (`--completed-hours`, `--failed-days`). Inside
those windows a redelivery is answered from the record; after them it would be processed (and
charged) again, so keep the failed window longer than any redelivery your clients can produce.
Stored results include the generated text.

### Error codes

Every error body is `{"detail": {"code": "...", "message": "..."}}`. Branch on `code`:

| HTTP | `code` | what the client should do |
|---|---|---|
| 401 | `unauthorized` | fix the credential |
| 503 | `api_disabled` | nothing is configured (no shared token, no active key); ask the operator |
| 403 | `tenant_mismatch` | the key belongs to another tenant; use that tenant's key |
| 403 | `no_budget` | no budget for this tenant; ask the operator |
| 402 | `budget_exceeded` | wait for the next UTC day or a higher budget; do not retry now |
| 409 | `no_checked_price` | the model has no current checked price; ask the operator, do not retry |
| 409 | `request_in_flight` | retry the same request id after `Retry-After` seconds |
| 409 | `request_id_spent` | retry with a new request id |
| 409 | `request_id_conflict` | the id was used for another request; use a new id |
| 422 | `invalid_request`, `tenant_required`, `invalid_tenant` | fix the request |
| 502 | `provider_rejected` | the vendor refused before any work; nothing was charged; retrying may help (rate limit) |
| 502 | `provider_failed` | the vendor failed and the call was charged at its worst case; retry with a new request id |

### Providers

| provider | how it was tested | usage mapping |
|---|---|---|
| `mock` | offline, every test | deterministic token counts |
| `gemini` | 10 live calls, recorded and replayed in CI (below) | `prompt_token_count` in; `candidates_token_count + thoughts_token_count` out |
| `openai` | **fake clients and the SDK's exception classes only; never called against the real API** | Chat Completions: `prompt_tokens` (+ 1.5 × `cache_write_tokens`) in, `completion_tokens` out (already includes reasoning tokens); Responses API: `input_tokens` (+ 1.5 × `cache_write_tokens`) / `output_tokens` |
| `anthropic` | **fake clients and the SDK's exception classes only; never called against the real API** | `input_tokens + 2 × cache_creation_input_tokens + cache_read_input_tokens` in; `output_tokens` out |

**Caching.** The price book has one input rate per model; vendors bill cache writes above it
and cache reads below it. The adapters charge cache writes at the highest multiplier the
vendors' prompt-caching pages gave when they were read on 2026-11-08: 2.5× at OpenAI (1.25× for
the default lifetime, 2.5× for the 12-hour one; OpenAI caches automatically) and 2× at Anthropic
(1.25× for 5 minutes, 2× for 1 hour; opt-in, never requested here). Cache reads are charged at
the full rate. Reservations scale the input bound by the same multiplier (2.5× for OpenAI, 2×
for Anthropic), so a cache write cannot exceed a reservation. Assumptions: OpenAI's
`cache_write_tokens` is read as part of `prompt_tokens`, like `cached_tokens` in the same
breakdown (the SDK docstring does not say so); and the multipliers can change. Under those
assumptions caching is over-counted, never under-counted.

**Tier, speed, region, storage.** OpenAI requests ask for `service_tier="default"` and
`store=False` (no stored prompts); Anthropic requests ask for `service_tier="standard_only"` and
`speed="standard"`. A response that reports another tier or speed was served at a price the
price book does not have: it is charged its full reservation and marked in the ledger. That
reservation is computed at standard prices, so a vendor that billed a premium anyway would still
be under-counted. Anthropic regions: with `SPEND_GUARD_ANTHROPIC_INFERENCE_GEO` set, that region
is requested and required; without it, the workspace default applies and region pricing is not
checked.

Every vendor error is classified by HTTP status: 400, 401, 403, 404, 409, 413, 422 and 429 mean
the vendor refused the request before generating anything, so the reservation is released;
any other status, a timeout or a dropped connection may have been billed, so the reservation is
charged in full. The SDKs' own retries are switched off, so one call is one HTTP request.

## Live probe (Gemini)

Ten fixed prompts through the full guard, `gemini-3.6-flash` on Vertex AI (EU location, thinking
level low), 2026-11-08, priced at 0.75 / 3.75 USD per million input / output tokens (the
published standard-tier price on that date):

| prompt | max out | input tokens (bound) | output tokens | reserved | charged |
|---|---|---|---|---|---|
| one word | 64 | 12 (124) | 60 | 0.000333 | 0.000234 |
| short fact | 128 | 16 (151) | 124 | 0.000594 | 0.000477 |
| list | 256 | 18 (149) | 239 | 0.001072 | 0.000910 |
| explain | 256 | 23 (161) | 252 | 0.001081 | 0.000963 |
| long ask, small cap | 128 | 23 (160) | 124 | 0.000600 | 0.000483 |
| code | 384 | 19 (166) | 327 | 0.001565 | 0.001241 |
| non-English | 128 | 16 (134) | 124 | 0.000581 | 0.000477 |
| arabic | 128 | 16 (147) | 124 | 0.000591 | 0.000477 |
| json | 64 | 28 (153) | 60 | 0.000355 | 0.000246 |
| reasoning | 256 | 34 (170) | 252 | 0.001088 | 0.000971 |
| **total** | | | | 0.007860 | 0.006479 |

- **The bound held for every call**: no overrun; 82% of the reserved total was charged. The
  input bound was 5 to 10 times the real count, which costs little because input is the cheap
  side.
- **Thinking tokens count against `max_output_tokens` on this model.** Nine of ten calls used
  the cap or came within 17 tokens of it, and most answers were cut short ("A database
  transaction guarantees"). The JSON request returned an empty text after 60 output tokens. A
  budget guard sees the cost correctly either way, but callers who set a tight cap to save
  money get truncated answers; the cap needs room for reasoning.
- The price used came from the vendor's public pricing page for the Gemini API; the Vertex AI
  price was not checked separately. The amounts are what the ledger computed, not an invoice.

The recording is in `evals/fixtures/live-probe.jsonl`. `make probe-replay` (part of `make
check`) replays it through a fresh ledger, at the time it was recorded so the price stays valid,
and fails on any overrun, replay miss or reconciliation difference. `make record-live`
re-records it (10 live calls, needs credentials).

## Configuration

| variable | default | meaning |
|---|---|---|
| `SPEND_GUARD_DATABASE_URL` | throwaway local server | PostgreSQL |
| `SPEND_GUARD_PROVIDER` | `mock` | `mock`, `gemini`, `openai`, `anthropic` or `replay` |
| `SPEND_GUARD_MAX_LIVE_CALLS` | `0` | live providers: hard cap on calls per process; `0` makes none |
| `SPEND_GUARD_OPENAI_API` | `chat` | `chat` (Chat Completions) or `responses`; credentials from `OPENAI_API_KEY` |
| `SPEND_GUARD_ANTHROPIC_INFERENCE_GEO` | unset | region to request and require; credentials from `ANTHROPIC_API_KEY` |
| `SPEND_GUARD_GEMINI_VERTEX` | unset | `true`: Vertex AI with Application Default Credentials and `GOOGLE_CLOUD_PROJECT`; else `GEMINI_API_KEY` |
| `SPEND_GUARD_GEMINI_LOCATION`, `SPEND_GUARD_GEMINI_THINKING_LEVEL` | unset | Vertex location, thinking level |
| `SPEND_GUARD_MAX_PRICE_AGE_DAYS` | `31` | older prices count as missing |
| `SPEND_GUARD_API_TOKEN` | unset | shared token for one trusted caller (API keys are created with the CLI) |
| `SPEND_GUARD_OTEL_CONSOLE` | unset | `true`: print spans to stdout |

## Limits

- **Only the Gemini adapter has met a real API.** The OpenAI and Anthropic adapters follow the
  vendors' documented SDK shapes and are tested with fake clients; their first real call may
  still reveal a mismatch. Errors without an HTTP status (for example missing credentials) are
  charged the worst case.
- **Text only.** The input bound assumes text; images, audio and cached-token discounts are not
  modelled. Prices are per model, one input and one output rate, no tiers by prompt length.
- **Days are UTC days, one currency.** No monthly budgets; prices in another currency than the
  ledger's are refused, not converted.
- **The shared token can name any tenant**, and a new tenant id gets the default budget; use
  per-tenant API keys for every client you do not fully trust. Keys have no expiry and no
  scopes beyond the tenant.
- **Triggers stop accidents, not the table owner.** The owner can disable a trigger. Run the
  service as a role with only `SELECT, INSERT` on `ledger` and `price_book` (DESIGN.md).
- **Worst-case reservations refuse calls that would have fit.** Tight budgets with large
  `max_output_tokens` pause early (see the demo).
- **Alerts are rows and a callback**, not e-mails or pages; wire `on_alert` to your own channel.
- **No rate limiting.** Budgets cap money, not request rates; put a proxy in front for that.
- **Not load-tested.** Correctness under concurrency is tested (16 threads, 48 attempts, no
  overshoot; 8 threads repeating one request id, one model call); throughput is not measured. CI runs PostgreSQL 17, local runs 16.

## License

Apache-2.0. See [LICENSE](LICENSE). Copyright 2026 Hossam Afifi.
