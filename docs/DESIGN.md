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

**Unknown usage is charged in full.** A provider error that may have been billed (timeout,
server error, a response without usage) settles at the reserved amount. Only errors that
certainly happened before any work (400/401/403/404/429) release the hold. Over-counting is the
safe direction for a budget. Abandoned reservations get the same treatment from `sweep`.

**Integer micro-units, rounded up.** No floats anywhere in money paths. Prices are per million
tokens in micros; a cost is `ceil((in × p_in + out × p_out) / 1e6)`.

**An input bound without a tokenizer.** UTF-8 bytes + 64 tokens is a safe upper bound for
tokenizers that cover at least one byte per token, and avoids shipping a tokenizer per vendor.
Measured on the live probe: 5 to 10 times the real count, on the cheap side of the price.

**Fail closed.** No global budget, or no budget for the tenant and no default (`tenant:*`): the
call is refused. The HTTP API refuses everything without a token, and cannot change prices or
budgets.

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
