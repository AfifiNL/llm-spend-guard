"""llm-spend-guard command line: prices, budgets, status, reconcile, sweep, demo, probe, serve."""

from __future__ import annotations

import argparse
import datetime as dt
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

from spend_guard.money import fmt, parse

if TYPE_CHECKING:
    from psycopg_pool import ConnectionPool

    from spend_guard.ledger import SpendLedger


def _pool() -> ConnectionPool[Any]:
    from spend_guard.db import make_pool

    return make_pool()


def _cmd_price_add(a: argparse.Namespace) -> int:
    from spend_guard.prices import add_price

    checked = dt.datetime.fromisoformat(a.checked_at)
    if checked.tzinfo is None:
        checked = checked.replace(tzinfo=dt.UTC)
    with _pool().connection() as conn:
        pid = add_price(
            conn,
            a.model,
            parse(a.input),
            parse(a.output),
            checked_at=checked,
            source=a.source,
            currency=a.currency,
        )
    print(f"price {pid} recorded for {a.model}")
    return 0


def _cmd_price_list(_: argparse.Namespace) -> int:
    from spend_guard.prices import list_prices

    with _pool().connection() as conn:
        for p in list_prices(conn):
            print(
                f"{p.model}: in {fmt(p.input_per_mtok_micros)} / out "
                f"{fmt(p.output_per_mtok_micros)} {p.currency} per 1M tokens, checked "
                f"{p.checked_at:%Y-%m-%d} ({p.source})"
            )
    return 0


def _cmd_budget_set(a: argparse.Namespace) -> int:
    scope = a.scope if a.scope == "global" else f"tenant:{a.scope}"
    with _pool().connection() as conn:
        conn.execute(
            "INSERT INTO budgets (scope, daily_limit_micros) VALUES (%s, %s) ON CONFLICT (scope)"
            " DO UPDATE SET daily_limit_micros = EXCLUDED.daily_limit_micros, updated_at = now()",
            (scope, parse(a.daily_limit)),
        )
    print(f"{scope}: daily limit {a.daily_limit}")
    return 0


def _ledger() -> SpendLedger:
    from spend_guard.ledger import SpendLedger

    return SpendLedger(_pool())


def _cmd_status(a: argparse.Namespace) -> int:
    for s in _ledger().status(a.tenant):
        print(
            f"{s.scope}: limit {fmt(s.limit_micros)}, reserved {fmt(s.reserved_micros)}, "
            f"settled {fmt(s.settled_micros)}, remaining {fmt(s.remaining_micros)}"
        )
    return 0


def _cmd_reconcile(a: argparse.Namespace) -> int:
    day = dt.date.fromisoformat(a.day) if a.day else dt.datetime.now(dt.UTC).date()
    problems = _ledger().reconcile(day)
    for p in problems:
        print("MISMATCH", p)
    print("ledger and counters agree" if not problems else f"{len(problems)} mismatch(es)")
    return 1 if problems else 0


def _cmd_sweep(a: argparse.Namespace) -> int:
    n = _ledger().sweep(dt.timedelta(minutes=a.older_than_minutes))
    print(f"charged {n} abandoned reservation(s) at their worst case")
    return 0


def _cmd_key_create(a: argparse.Namespace) -> int:
    from spend_guard.keys import create_key

    with _pool().connection() as conn:
        key, key_id = create_key(conn, a.tenant)
    print(f"key {key_id} for tenant {a.tenant} (shown once, store it now):")
    print(key)
    return 0


def _cmd_key_revoke(a: argparse.Namespace) -> int:
    from spend_guard.keys import revoke_key

    with _pool().connection() as conn:
        n = revoke_key(conn, a.key_id)
    print(f"revoked {n} key(s)")
    return 0 if n else 1


def _cmd_key_list(_: argparse.Namespace) -> int:
    from spend_guard.keys import list_keys

    with _pool().connection() as conn:
        for k in list_keys(conn):
            state = f"revoked {k.revoked_at:%Y-%m-%d}" if k.revoked_at else "active"
            print(f"{k.key_id}  {k.tenant}  created {k.created_at:%Y-%m-%d}  {state}")
    return 0


def _cmd_idem_prune(a: argparse.Namespace) -> int:
    from spend_guard.idempotency import IdempotencyStore

    store = IdempotencyStore(_pool(), clock=lambda: dt.datetime.now(dt.UTC))
    swept = store.sweep_stale()
    n = store.prune(
        keep_completed=dt.timedelta(hours=a.completed_hours),
        keep_failed=dt.timedelta(days=a.failed_days),
    )
    print(f"marked {swept} abandoned request(s) as failed; removed {n} finished record(s)")
    return 0


def _cmd_demo(_: argparse.Namespace) -> int:
    from spend_guard.demo import run_demo

    return run_demo()


def probe_clock(meta: dict[str, Any]) -> dt.datetime:
    """A replay runs at the time it was recorded, so the price that was checked then is still
    "fresh" for it, however long after the recording CI runs."""
    stamp = str(meta.get("recorded_at", ""))
    if not stamp:
        raise ValueError("cassette has no recorded_at; re-record it")
    return dt.datetime.fromisoformat(stamp.replace("Z", "+00:00")) + dt.timedelta(minutes=1)


def _cmd_probe(a: argparse.Namespace) -> int:
    import uuid

    from spend_guard.db import drop_schema, make_pool
    from spend_guard.factory import build_provider
    from spend_guard.gateway import SpendGuard
    from spend_guard.ledger import SpendLedger
    from spend_guard.probe import run_probe, seed_prices, table
    from spend_guard.providers.replay import RecordingProvider, ReplayProvider

    schema = f"spend_guard_probe_{uuid.uuid4().hex[:8]}"
    pool = make_pool(schema=schema)  # a scratch schema: never the real budgets or ledger
    try:
        with pool.connection() as conn:
            seed_prices(conn, Path(a.prices))
            conn.execute(
                "INSERT INTO budgets (scope, daily_limit_micros) VALUES ('global', %s),"
                " ('tenant:probe', %s)",
                (parse("1.00"), parse("1.00")),
            )
        provider = build_provider()
        clock_now = dt.datetime.now(dt.UTC)
        if isinstance(provider, ReplayProvider):
            clock_now = probe_clock(provider.meta)
        record = os.environ.get("SPEND_GUARD_RECORD_CASSETTE")
        if record:
            meta = {
                "model": "gemini-3.6-flash",
                "thinking_level": os.environ.get("SPEND_GUARD_GEMINI_THINKING_LEVEL", ""),
                "location": os.environ.get("SPEND_GUARD_GEMINI_LOCATION", ""),
                "recorded_at": clock_now.strftime("%Y-%m-%dT%H:%M:00Z"),
            }
            provider = RecordingProvider(provider, Path(record), meta)
        ledger = SpendLedger(pool, clock=lambda: clock_now)
        rows = run_probe(SpendGuard(ledger, provider))
        print(table(rows))
        if isinstance(provider, RecordingProvider):
            provider.save()
            print(f"recorded {len(provider.rows)} calls to {record}")
        if isinstance(provider, ReplayProvider) and provider.misses:
            print(f"REPLAY MISS: {provider.misses}")
            return 1
        problems = ledger.reconcile(clock_now.date())
        over = [r.label for r in rows if r.cost_micros > r.reserved_micros]
        if problems or over:
            print(f"FAILED: reconcile {problems}, overruns {over}")
            return 1
        print("probe passed: every call settled within its reservation; ledger reconciles")
        return 0
    finally:
        drop_schema(pool, schema)
        pool.close()


def _cmd_serve(a: argparse.Namespace) -> int:  # pragma: no cover - runs a server
    import uvicorn

    from spend_guard.api import app_from_env

    uvicorn.run(app_from_env(), host=a.host, port=a.port)
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="llm-spend-guard", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    pa = sub.add_parser("price-add", help="record a checked price (per 1M tokens)")
    pa.add_argument("model")
    pa.add_argument("--input", required=True, help="price per 1M input tokens, e.g. 0.75")
    pa.add_argument("--output", required=True, help="price per 1M output tokens, e.g. 3.75")
    pa.add_argument("--checked-at", required=True, help="ISO date-time you checked it")
    pa.add_argument("--source", required=True, help="where you checked it")
    pa.add_argument("--currency", default="USD")
    pa.set_defaults(fn=_cmd_price_add)
    sub.add_parser("price-list", help="current price per model").set_defaults(fn=_cmd_price_list)
    b = sub.add_parser("budget-set", help="daily limit for 'global', a tenant id, or '*'")
    b.add_argument("scope")
    b.add_argument("daily_limit")
    b.set_defaults(fn=_cmd_budget_set)
    s = sub.add_parser("status", help="today's spend for a tenant")
    s.add_argument("tenant")
    s.set_defaults(fn=_cmd_status)
    r = sub.add_parser("reconcile", help="recompute counters from the ledger")
    r.add_argument("--day")
    r.set_defaults(fn=_cmd_reconcile)
    w = sub.add_parser("sweep", help="charge abandoned reservations at their worst case")
    w.add_argument("--older-than-minutes", type=int, default=15)
    w.set_defaults(fn=_cmd_sweep)
    kc = sub.add_parser("key-create", help="create an API key bound to one tenant")
    kc.add_argument("tenant")
    kc.set_defaults(fn=_cmd_key_create)
    kr = sub.add_parser("key-revoke", help="revoke a key by its id (first 8+ hex of its hash)")
    kr.add_argument("key_id")
    kr.set_defaults(fn=_cmd_key_revoke)
    sub.add_parser("key-list", help="list keys (ids and tenants, never the keys)").set_defaults(
        fn=_cmd_key_list
    )
    ip = sub.add_parser(
        "idempotency-prune",
        help="mark abandoned requests failed; forget completed ids after --completed-hours and"
        " failed ones (may have been billed) after --failed-days",
    )
    ip.add_argument("--completed-hours", type=int, default=24)
    ip.add_argument("--failed-days", type=int, default=30)
    ip.set_defaults(fn=_cmd_idem_prune)
    sub.add_parser("demo", help="offline demo with the mock model").set_defaults(fn=_cmd_demo)
    pr = sub.add_parser("probe", help="fixed prompts through the guard; checks the bound")
    pr.add_argument("--prices", default="evals/prices.json")
    pr.set_defaults(fn=_cmd_probe)
    sv = sub.add_parser("serve", help="HTTP API")
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=8000)
    sv.set_defaults(fn=_cmd_serve)
    args = p.parse_args(argv)
    code: int = args.fn(args)
    return code


if __name__ == "__main__":
    sys.exit(main())
