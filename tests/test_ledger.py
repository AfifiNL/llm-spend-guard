from __future__ import annotations

import datetime as dt
import threading
from typing import Any

import psycopg
import pytest
from psycopg_pool import ConnectionPool

from conftest import NOW, Clock, setup_prices_and_budgets
from spend_guard.errors import BudgetExceeded, NoBudget, NoCheckedPrice, UnknownReservation
from spend_guard.ledger import Alert, SpendLedger
from spend_guard.prices import add_price

# price: 1.0 per Mtok in, 2.0 per Mtok out (micros per Mtok: 1_000_000 / 2_000_000)
# so 1,000 in + 1,000 out tokens = 1,000 + 2,000 = 3,000 micros.


def test_reserve_then_settle_charges_actual_usage(ledger: SpendLedger) -> None:
    r = ledger.reserve("acme", "m1", max_input_tokens=1000, max_output_tokens=1000)
    assert r.reserved_micros == 3000
    s = ledger.settle(r.request_id, input_tokens=400, output_tokens=100)
    assert s.cost_micros == 600
    assert not s.overrun
    g, t = ledger.status("acme")
    assert (g.scope, g.reserved_micros, g.settled_micros) == ("global", 0, 600)
    assert (t.scope, t.limit_micros, t.settled_micros) == ("tenant:acme", 500_000, 600)
    assert ledger.reconcile(NOW.date()) == []


def test_costs_round_up(ledger: SpendLedger) -> None:
    r = ledger.reserve("acme", "m1", max_input_tokens=1, max_output_tokens=1)
    assert r.reserved_micros == 3  # 1 + 2 micros exactly
    assert ledger.settle(r.request_id, 1, 0).cost_micros == 1


def test_no_checked_price_no_call(pool: ConnectionPool[Any], clock: Clock) -> None:
    setup_prices_and_budgets(pool)
    ledger = SpendLedger(pool, clock=clock, max_price_age=dt.timedelta(days=30))
    with pytest.raises(NoCheckedPrice, match="no checked price"):
        ledger.reserve("acme", "unknown-model", max_input_tokens=1, max_output_tokens=1)
    with pool.connection() as conn:
        add_price(conn, "old", 1, 1, checked_at=NOW - dt.timedelta(days=45), source="x")
        add_price(conn, "future", 1, 1, checked_at=NOW + dt.timedelta(days=1), source="x")
    with pytest.raises(NoCheckedPrice, match="check it again"):
        ledger.reserve("acme", "old", max_input_tokens=1, max_output_tokens=1)
    with pytest.raises(NoCheckedPrice):  # a price dated in the future is not checked yet
        ledger.reserve("acme", "future", max_input_tokens=1, max_output_tokens=1)
    with pool.connection() as conn, pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(
            "INSERT INTO price_book (model, input_per_mtok_micros, output_per_mtok_micros,"
            " checked_at, source) VALUES ('m', 1, 1, now(), ' ')"
        )


def test_newest_price_wins_and_settlement_uses_the_reserved_price(
    pool: ConnectionPool[Any], clock: Clock
) -> None:
    setup_prices_and_budgets(pool)
    ledger = SpendLedger(pool, clock=clock)
    r = ledger.reserve("acme", "m1", max_input_tokens=1000, max_output_tokens=0 + 1)
    with pool.connection() as conn:
        add_price(conn, "m1", 10_000_000, 20_000_000, checked_at=NOW, source="price rise")
    # The call was reserved at the old price and is settled at the old price.
    assert ledger.settle(r.request_id, 1000, 0).cost_micros == 1000
    r2 = ledger.reserve("acme", "m1", max_input_tokens=1000, max_output_tokens=1)
    assert r2.reserved_micros == 10_000 + 20


def test_fail_closed_without_budgets(pool: ConnectionPool[Any], clock: Clock) -> None:
    setup_prices_and_budgets(pool, tenant_default=None, tenants={"acme": 10_000})
    ledger = SpendLedger(pool, clock=clock)
    ledger.reserve("acme", "m1", max_input_tokens=1, max_output_tokens=1)
    with pytest.raises(NoBudget, match="tenant:other"):
        ledger.reserve("other", "m1", max_input_tokens=1, max_output_tokens=1)
    with pool.connection() as conn:
        conn.execute("DELETE FROM budgets WHERE scope = 'global'")
    with pytest.raises(NoBudget, match="global"):
        ledger.reserve("acme", "m1", max_input_tokens=1, max_output_tokens=1)


def test_budget_is_checked_against_the_worst_case(ledger: SpendLedger) -> None:
    # tenant default 500_000 micros. 100k in + 200k out = 100k + 400k = 500k: fits exactly.
    r = ledger.reserve("acme", "m1", max_input_tokens=100_000, max_output_tokens=200_000)
    with pytest.raises(BudgetExceeded) as info:
        ledger.reserve("acme", "m1", max_input_tokens=1, max_output_tokens=1)
    assert info.value.scope == "tenant:acme"
    # Settling the real (smaller) usage frees the rest of the hold.
    ledger.settle(r.request_id, 1000, 1000)
    ledger.reserve("acme", "m1", max_input_tokens=1, max_output_tokens=1)


def test_global_budget_caps_all_tenants(pool: ConnectionPool[Any], clock: Clock) -> None:
    setup_prices_and_budgets(pool, global_limit=6000, tenant_default=5000)
    ledger = SpendLedger(pool, clock=clock)
    ledger.reserve("a", "m1", max_input_tokens=1000, max_output_tokens=1000)  # 3000
    ledger.reserve("b", "m1", max_input_tokens=1000, max_output_tokens=1000)  # 6000 total
    with pytest.raises(BudgetExceeded) as info:
        ledger.reserve("c", "m1", max_input_tokens=1, max_output_tokens=1)
    assert info.value.scope == "global"


def test_alerts_at_80_percent_and_on_pause(pool: ConnectionPool[Any], clock: Clock) -> None:
    setup_prices_and_budgets(pool, global_limit=10_000_000, tenant_default=10_000)
    seen: list[Alert] = []
    ledger = SpendLedger(pool, clock=clock, on_alert=seen.append)
    ledger.reserve("acme", "m1", max_input_tokens=7000, max_output_tokens=1)  # 7002: < 80%
    assert seen == []
    ledger.reserve("acme", "m1", max_input_tokens=1000, max_output_tokens=1)  # 8004: >= 80%
    assert [(a.scope, a.level) for a in seen] == [("tenant:acme", "warn")]
    ledger.reserve("acme", "m1", max_input_tokens=100, max_output_tokens=1)  # warn only once
    assert len(seen) == 1
    with pytest.raises(BudgetExceeded):
        ledger.reserve("acme", "m1", max_input_tokens=5000, max_output_tokens=1)
    assert [(a.scope, a.level) for a in seen] == [
        ("tenant:acme", "warn"),
        ("tenant:acme", "pause"),
    ]
    assert [a.level for a in ledger.alerts()] == ["warn", "pause"]


def test_new_day_new_budget(ledger: SpendLedger, clock: Clock) -> None:
    ledger.reserve("acme", "m1", max_input_tokens=100_000, max_output_tokens=200_000)
    with pytest.raises(BudgetExceeded):
        ledger.reserve("acme", "m1", max_input_tokens=1, max_output_tokens=1)
    clock.now = NOW + dt.timedelta(days=1)
    ledger.reserve("acme", "m1", max_input_tokens=1, max_output_tokens=1)


def test_release_and_worst_case(ledger: SpendLedger) -> None:
    r1 = ledger.reserve("acme", "m1", max_input_tokens=1000, max_output_tokens=1000)
    assert ledger.release(r1.request_id, "rate limited").cost_micros == 0
    r2 = ledger.reserve("acme", "m1", max_input_tokens=1000, max_output_tokens=1000)
    assert ledger.settle_worst_case(r2.request_id, "timeout").cost_micros == 3000
    with pytest.raises(UnknownReservation):
        ledger.settle(r2.request_id, 1, 1)
    with pytest.raises(UnknownReservation):
        ledger.release(r1.request_id)
    assert ledger.status("acme")[1].settled_micros == 3000
    assert ledger.reconcile(NOW.date()) == []


def test_overrun_is_recorded_not_hidden(ledger: SpendLedger, pool: ConnectionPool[Any]) -> None:
    r = ledger.reserve("acme", "m1", max_input_tokens=10, max_output_tokens=10)
    s = ledger.settle(r.request_id, 1000, 1000)
    assert s.overrun
    with pool.connection() as conn:
        note = conn.execute("SELECT note FROM ledger WHERE kind = 'settle'").fetchone()
    assert note is not None
    assert "overrun" in note[0]
    assert ledger.reconcile(NOW.date()) == []


def test_sweep_charges_abandoned_reservations(ledger: SpendLedger, clock: Clock) -> None:
    r = ledger.reserve("acme", "m1", max_input_tokens=1000, max_output_tokens=1000)
    assert ledger.sweep(dt.timedelta(minutes=10)) == 0
    clock.now = NOW + dt.timedelta(minutes=11)
    assert ledger.sweep(dt.timedelta(minutes=10)) == 1
    with pytest.raises(UnknownReservation):
        ledger.settle(r.request_id, 1, 1)


def test_ledger_is_append_only(ledger: SpendLedger, pool: ConnectionPool[Any]) -> None:
    ledger.reserve("acme", "m1", max_input_tokens=1, max_output_tokens=1)
    with pool.connection() as conn:
        for sql in ("UPDATE ledger SET amount_micros = 0", "DELETE FROM ledger", "TRUNCATE ledger"):
            with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
                conn.execute(sql)


def test_reconcile_detects_tampered_counters(
    ledger: SpendLedger, pool: ConnectionPool[Any]
) -> None:
    r = ledger.reserve("acme", "m1", max_input_tokens=1000, max_output_tokens=1000)
    ledger.settle(r.request_id, 10, 10)
    with pool.connection() as conn:
        conn.execute("UPDATE spend_day SET settled_micros = 0 WHERE scope = 'global'")
    assert ledger.reconcile(NOW.date()) == ["global: ledger says (0, 30), counters say (0, 0)"]


def test_concurrent_reservations_never_overshoot(pool: ConnectionPool[Any], clock: Clock) -> None:
    # Each call reserves 3,000 micros; the tenant may hold 10 of them (30,000).
    setup_prices_and_budgets(pool, global_limit=10_000_000, tenant_default=30_000)
    ledger = SpendLedger(pool, clock=clock)
    ok: list[int] = []
    refused: list[int] = []
    barrier = threading.Barrier(16)

    def worker(i: int) -> None:
        barrier.wait()
        for _ in range(3):
            try:
                ledger.reserve("acme", "m1", max_input_tokens=1000, max_output_tokens=1000)
                ok.append(i)
            except BudgetExceeded:
                refused.append(i)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(ok) == 10
    assert len(refused) == 38
    assert ledger.status("acme")[1].reserved_micros == 30_000
    assert ledger.reconcile(NOW.date()) == []
