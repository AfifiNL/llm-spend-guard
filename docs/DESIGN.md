# Design notes

## Goals, in priority order

1. A budget limit holds, also under concurrency: no sequence of allowed calls takes a scope past
   its limit (unless a provider reports more usage than the caller's own bound, which is
   recorded, never hidden).
2. No call is made without a price someone checked, recently, with a source.
3. Every cent is explainable from an append-only ledger.
4. Correctness is tested against a real PostgreSQL, offline, at zero cost.
5. Providers are replaceable; spend data reaches any tracing backend through OpenTelemetry.

## Decisions

**Reserve the worst case, settle the actual.** Checking "spent so far" before a call is not
enough: ten concurrent calls can all see the same remaining budget. Holding the worst case in
the same transaction as the check makes the limit hold. Settling replaces the hold with the
reported usage, so the over-reservation is temporary. The price is refusing some calls that
would have fit (README demo, call 12).

**Row locks in one fixed order.** The day's counters for the global and the tenant scope are
locked with `SELECT … FOR UPDATE`, always sorted by scope name, so two transactions cannot
deadlock. Missing counter rows are created with `INSERT … ON CONFLICT DO NOTHING` first.
Tested with 16 threads racing for 10 slots: exactly 10 succeed.

**Counters are a cache; the ledger is the truth.** `spend_day` makes the check one indexed
row per scope. The ledger records every reserve, settle and release, including the amount each
settle released, so `reconcile()` can rebuild every counter and report a difference. Triggers
reject `UPDATE`, `DELETE` and `TRUNCATE` on the ledger.

**Prices are rows with a date and a source, never edited.** The newest row wins, a row dated in
the future is not used yet, and a row older than `max_price_age` counts as missing. A
reservation stores the price id, and settlement uses that same price even if a newer one was
added in between. During development the guard refused the live probe once because the price
had been dated a few minutes in the future: the rule works as intended, including against its
author.

**Alerts follow money, refusals follow holds.** The 80% warning compares settled spend with the
limit: a large temporary hold is not spending. A refusal records a `pause` alert at once, and so
does settled spend reaching the limit. Alerts are rows written in the same transaction, and the
callback runs after commit; a failing callback is logged and never undoes or hides a charge.

**Unknown usage is charged in full.** A provider error that may have been billed (timeout,
server error, a response without usage) settles at the reserved amount. Only errors that
certainly happened before any work (400/401/403/404/429) release the hold. Over-counting is the
safe direction for a budget. Abandoned reservations get the same treatment from `sweep`. A call
that settles after its reservation was swept still records its real usage: anything above the
worst case is charged and marked as an overrun, and nothing below it is refunded.

**Who may write.** Triggers reject `UPDATE`, `DELETE` and `TRUNCATE` on the ledger and edits to
the price book, but a table owner can disable triggers. In production, migrate as an owner role
and run the service as another:

```sql
GRANT SELECT, INSERT ON ledger, price_book, reservations, alerts TO spend_guard_app;
GRANT SELECT, INSERT, UPDATE ON spend_day, reservations TO spend_guard_app;
GRANT SELECT ON budgets, api_keys TO spend_guard_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON idempotency TO spend_guard_app;  -- prune deletes
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO spend_guard_app;
```

**Integer micro-units, rounded up.** No floats anywhere in money paths. Prices are per million
tokens in micros; a cost is `ceil((in × p_in + out × p_out) / 1e6)`.

**An input bound without a tokenizer.** UTF-8 bytes + 64 tokens is a safe upper bound for
tokenizers that cover at least one byte per token, and avoids shipping a tokenizer per vendor.
Measured on the live probe: 5 to 10 times the real count, on the cheap side of the price. Providers that
bill input above the base rate (cache writes) declare a ceiling, and the bound is multiplied
by it.

**Fail closed.** No global budget, or no budget for the tenant and no default (`tenant:*`): the
call is refused. The HTTP API refuses everything without a token, and cannot change prices or
budgets.

**Keys decide the tenant.** A shared token lets its holder name any tenant, so budgets per
tenant only mean something when each client has its own credential. An API key is bound to one
tenant at creation; the HTTP layer derives the tenant from it and refuses a request that names
another. Only the SHA-256 of a key is stored (keys are 256-bit random, so a plain hash is
enough; no salt or slow hash is needed against guessing), and the plaintext is printed once.
Keys are created and revoked with the CLI only, like prices and budgets.

**One request id, one charge.** Clients retry: their own timeouts, at-least-once queues. A
client `request_id` is keyed per tenant, so ids cannot collide or leak across tenants. It is
claimed with `INSERT … ON CONFLICT DO NOTHING`, so of several concurrent requests with one key
exactly one proceeds and the rest get "in flight" (409 with `Retry-After`). A completed request
stores its result and replays it with `charged_now: 0`. The key is freed only when nothing was
charged (a refusal before the call, or a provider that rejected the request before any work).
After a failure that may have been billed, or a request in flight for more than 15 minutes (a
crash, or a completion that could not be recorded), the key is "spent": 409 with its own code,
and the client must use a new id. Failed keys are kept 30 days by default, completed ones 24
hours: within those windows a late redelivery is refused or replayed, never charged again. The
reservation has its own id, so a freed client id can be reserved again. If the result of a
completed call cannot be recorded, the caller still gets the answer; the key falls into "spent"
later rather than ever running twice.

**Machine-readable errors.** Several outcomes share an HTTP status (409: in flight, id spent, id
conflict, no checked price). Each error carries a stable `code`, and only `request_in_flight`
carries `Retry-After`, so a client can tell "retry the same id later" from "use a new id" from
"do not retry".

**Vendor adapters: classify by status, retries off.** Every SDK error is reduced to its type and
HTTP status (vendor messages can echo request content). Statuses that mean "refused before
generating" (400, 401, 403, 404, 409, 413, 422, 429) release the reservation; everything else,
including timeouts and dropped connections, is charged in full. The SDKs' automatic retries are
switched off: a hidden retry after a timeout could be billed twice for one counted call. Tests
use the installed SDKs' own exception classes, not look-alikes.

**Premiums are priced or charged in full.** Vendors bill cache writes above the input rate,
and premium tiers or speeds above the standard price. With one input and one output rate per
model in the price book, cache writes are converted at the highest published multiplier (2.5×
OpenAI, 2× Anthropic, as read on 2026-11-08) and reservations scale the input bound by the same
factor. Requests ask for the standard tier and speed explicitly; a response served otherwise is
charged its full reservation and marked, because its real price is unknown. A price book with
separate cache and tier rates would be exact; the guarantee does not need it, and the README
states the assumptions it rests on.

**Telemetry by convention.** Spans use the OpenTelemetry GenAI attribute names where they exist
(`gen_ai.request.model`, `gen_ai.usage.*`) and a `spend.*` namespace for the rest, so the cost
of each call shows up next to its latency in any tracing backend.

**Record once, replay through the real ledger.** The live probe's provider responses (text and
usage) are recorded. CI replays them through a fresh database and the full reserve/settle path,
so the settlement maths runs on real usage numbers on every push, for free.

## Calibration

| setting | value | why |
|---|---|---|
| warn ratio | 0.8 | conventional early warning; configurable |
| max price age | 31 days | prices change; a monthly re-check is a cheap habit |
| input allowance | 64 tokens | chat formatting adds a few tokens per message; measured inputs were 12 to 34 tokens for 60 to 106 bytes |
| API `max_output_tokens` | 1 to 4096 | bounds the worst case a single request can reserve |
