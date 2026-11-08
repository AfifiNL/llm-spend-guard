# llm-spend-guard

A budget guard for LLM calls, in Python on PostgreSQL. Before each call it reserves the call's
worst-case cost against a daily global budget and a daily per-tenant budget; after the call it
settles the actual cost from the provider's reported usage. A call that could take a budget past
its limit is refused before it is made, and so is a call to a model without a checked price.

- **Reserve, then settle**, in transactions that lock the day's counters: concurrent calls can
  never both fit into the same remaining budget.
- **No checked price, no call**: every price is a dated, sourced row; a missing, stale or
  future-dated price refuses the call.
- **Alerts** once per scope and day at 80% (configurable), and a **pause** record when a call is
  refused.
- **Append-only ledger** (enforced by triggers), running totals for fast checks, and a
  `reconcile` that recomputes the totals from the ledger.
- **OpenTelemetry spans** per call with the GenAI attributes and the reserved and charged
  amounts, for any exporter.
- **Mock provider**, so the demo and the tests spend nothing; a Gemini adapter; and a recorded
  live probe that CI replays through a real ledger.

## What it does

`make demo` (offline, mock model, a throwaway local PostgreSQL):

```text
== price: mock-small 2.00 in / 8.00 out per 1M tokens; budget per tenant 0.005/day
== team-212517 calls the model (max 200 output tokens each)
   call  1: reserved 0.001906  charged 0.000296
   ...
   call  9: reserved 0.001906  charged 0.000296
           ALERT warn on tenant:team-212517: 0.004274 of 0.005000
   call 10: reserved 0.001906  charged 0.000296
   call 11: reserved 0.001906  charged 0.000296
   call 12: REFUSED before the call: tenant:team-212517: 0.003256 spent or reserved + 0.001906 worst case for this call > 0.005000 daily limit
           ALERT pause on tenant:team-212517: 0.003256 of 0.005000
== a model without a checked price
   REFUSED: no checked price for model 'mock-large': add one to the price book
== trace spans (any OpenTelemetry exporter can ship these)
   llm.complete: outcome=ok reserved=1906 cost=296 model=mock-small
   llm.complete: outcome=refused_budget reserved=None cost=None model=mock-small
   llm.complete: outcome=refused_no_price reserved=None cost=None model=mock-large
== reconcile: ledger and counters agree
```

Call 12 shows the trade-off this design makes on purpose: the tenant had spent 0.003256 of
0.005, but the call's worst case (0.001906) no longer fit, so it was refused. A guarantee that
the limit holds costs some headroom.

## Quick start

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/). No database setup: without
`SPEND_GUARD_DATABASE_URL`, a pip-packaged PostgreSQL is started in a temporary directory.

```bash
make install    # .venv with extras, dev tools and the local PostgreSQL (locked)
make demo       # the run above
make check      # lint, strict types, 43 tests on PostgreSQL, probe replay, demo (what CI runs)
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
  worst case by `llm-spend-guard sweep`.

### HTTP API

| Method | Path | Result |
|---|---|---|
| `POST` | `/v1/complete` | `{tenant, model, prompt, system?, max_output_tokens}` → text, usage, reserved, cost; **402** budget, **409** no checked price, **403** no budget configured, **502** provider error |
| `GET` | `/v1/budgets/{tenant}` | today's limit, reserved, settled and remaining per scope |
| `GET` | `/healthz` | liveness |

Both `/v1` routes need `Authorization: Bearer $SPEND_GUARD_API_TOKEN`; without the variable they
refuse everything. Prices and budgets can only be changed with the CLI, not over HTTP.

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
check`) replays it through a fresh ledger in CI and fails on any overrun, replay miss or
reconciliation difference. `make record-live` re-records it (10 live calls, needs credentials).

## Configuration

| variable | default | meaning |
|---|---|---|
| `SPEND_GUARD_DATABASE_URL` | throwaway local server | PostgreSQL |
| `SPEND_GUARD_PROVIDER` | `mock` | `mock`, `gemini` or `replay` |
| `SPEND_GUARD_MAX_LIVE_CALLS` | `0` | Gemini: hard cap on live calls per process; `0` makes none |
| `SPEND_GUARD_GEMINI_VERTEX` | unset | `true`: Vertex AI with Application Default Credentials and `GOOGLE_CLOUD_PROJECT`; else `GEMINI_API_KEY` |
| `SPEND_GUARD_GEMINI_LOCATION`, `SPEND_GUARD_GEMINI_THINKING_LEVEL` | unset | Vertex location, thinking level |
| `SPEND_GUARD_MAX_PRICE_AGE_DAYS` | `31` | older prices count as missing |
| `SPEND_GUARD_API_TOKEN` | unset | required for the HTTP API |
| `SPEND_GUARD_OTEL_CONSOLE` | unset | `true`: print spans to stdout |

## Limits

- **One provider adapter (Gemini) besides the mock.** The provider interface is three lines;
  OpenAI and Anthropic adapters are not included and nothing here was run against them.
- **Text only.** The input bound assumes text; images, audio and cached-token discounts are not
  modelled. Prices are per model, one input and one output rate, no tiers by prompt length.
- **Days are UTC days.** No monthly budgets, no per-user or per-key budgets beyond tenants.
- **Worst-case reservations refuse calls that would have fit.** Tight budgets with large
  `max_output_tokens` pause early (see the demo).
- **Alerts are rows and a callback**, not e-mails or pages; wire `on_alert` to your own channel.
- **One shared API token**; put your own authentication in front before exposing it.
- **Not load-tested.** Correctness under concurrency is tested (16 threads, 48 attempts, no
  overshoot); throughput is not measured.

## License

Apache-2.0. See [LICENSE](LICENSE). Copyright 2026 Hossam Afifi.
