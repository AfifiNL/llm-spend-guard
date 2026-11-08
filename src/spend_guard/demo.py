"""Offline demo: a mock model behind the guard, one tenant spending its daily budget until the
guard warns at 80% and pauses, a model with no checked price refused, then the ledger, the
trace spans and a reconciliation. Uses a throwaway local PostgreSQL unless
SPEND_GUARD_DATABASE_URL is set (then it uses that database)."""

from __future__ import annotations

import datetime as dt

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from spend_guard.db import make_pool
from spend_guard.errors import BudgetExceeded, NoCheckedPrice
from spend_guard.gateway import SpendGuard
from spend_guard.ledger import Alert, SpendLedger
from spend_guard.money import fmt, parse
from spend_guard.prices import add_price
from spend_guard.providers.mock import MockProvider

PROMPT = "Summarise the attached support conversation in three bullet points for the account owner."


def run_demo() -> int:
    pool = make_pool()
    now = dt.datetime.now(dt.UTC)
    with pool.connection() as conn:
        add_price(
            conn,
            "mock-small",
            parse("2.00"),
            parse("8.00"),
            checked_at=now,
            source="demo price for the offline mock model, not a real tariff",
        )
        for scope, limit in (("global", "0.050"), ("tenant:*", "0.005")):
            conn.execute(
                "INSERT INTO budgets (scope, daily_limit_micros) VALUES (%s, %s)"
                " ON CONFLICT (scope) DO UPDATE"
                " SET daily_limit_micros = EXCLUDED.daily_limit_micros",
                (scope, parse(limit)),
            )
    exporter = InMemorySpanExporter()
    tp = TracerProvider()
    tp.add_span_processor(SimpleSpanProcessor(exporter))
    alerts: list[Alert] = []
    ledger = SpendLedger(pool, on_alert=alerts.append)
    tenant = f"team-{now:%H%M%S}"
    guard = SpendGuard(ledger, MockProvider(), tracer=tp.get_tracer("demo"))

    print("== price: mock-small 2.00 in / 8.00 out per 1M tokens; budget per tenant 0.005/day")
    print(f"== {tenant} calls the model (max 200 output tokens each)")
    for i in range(1, 13):
        n_alerts = len(alerts)
        try:
            c = guard.complete(tenant, "mock-small", PROMPT, max_output_tokens=200)
            line = f"   call {i:2}: reserved {fmt(c.reserved_micros)}  charged {fmt(c.cost_micros)}"
        except BudgetExceeded as exc:
            line = f"   call {i:2}: REFUSED before the call: {exc}"
        for a in alerts[n_alerts:]:
            line += (
                f"\n           ALERT {a.level} on {a.scope}: "
                f"{fmt(a.spent_micros)} of {fmt(a.limit_micros)}"
            )
        print(line)
    print("== a model without a checked price")
    try:
        guard.complete(tenant, "mock-large", PROMPT, max_output_tokens=200)
    except NoCheckedPrice as exc:
        print(f"   REFUSED: {exc}")
    print("== status")
    for s in ledger.status(tenant):
        print(
            f"   {s.scope}: settled {fmt(s.settled_micros)}, reserved {fmt(s.reserved_micros)},"
            f" remaining {fmt(s.remaining_micros)} of {fmt(s.limit_micros)}"
        )
    print("== trace spans (any OpenTelemetry exporter can ship these)")
    for span in exporter.get_finished_spans()[-3:]:
        attrs = dict(span.attributes or {})
        print(
            f"   {span.name}: outcome={attrs.get('spend.outcome')} "
            f"reserved={attrs.get('spend.reserved_micros')} cost={attrs.get('spend.cost_micros')} "
            f"model={attrs.get('gen_ai.request.model')}"
        )
    problems = ledger.reconcile(now.date())
    print("== reconcile: " + ("ledger and counters agree" if not problems else str(problems)))
    pool.close()
    return 1 if problems else 0
