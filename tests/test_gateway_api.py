from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from psycopg_pool import ConnectionPool

from conftest import NOW, Clock, setup_prices_and_budgets
from spend_guard.api import create_app
from spend_guard.errors import BudgetExceeded, NoCheckedPrice
from spend_guard.gateway import FORMAT_ALLOWANCE_TOKENS, SpendGuard, max_input_tokens
from spend_guard.ledger import SpendLedger
from spend_guard.probe import run_probe, seed_prices
from spend_guard.providers.base import ProviderFailed, ProviderRejected, ProviderResult
from spend_guard.providers.mock import MockProvider
from spend_guard.providers.replay import ReplayProvider

ROOT = Path(__file__).resolve().parents[1]


class Scripted:
    name = "scripted"

    def __init__(self, result: ProviderResult | Exception) -> None:
        self.result = result
        self.calls = 0

    def complete(
        self, model: str, system: str, prompt: str, max_output_tokens: int
    ) -> ProviderResult:
        self.calls += 1
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def make(
    pool: ConnectionPool[Any], clock: Clock, provider: Any
) -> tuple[SpendGuard, InMemorySpanExporter]:
    setup_prices_and_budgets(pool)
    exporter = InMemorySpanExporter()
    tp = TracerProvider()
    tp.add_span_processor(SimpleSpanProcessor(exporter))
    guard = SpendGuard(SpendLedger(pool, clock=clock), provider, tracer=tp.get_tracer("t"))
    return guard, exporter


def test_input_bound() -> None:
    assert max_input_tokens("ab", "cé") == 2 + 3 + FORMAT_ALLOWANCE_TOKENS


def test_successful_call_settles_and_traces(pool: ConnectionPool[Any], clock: Clock) -> None:
    guard, spans = make(pool, clock, Scripted(ProviderResult("hi", 100, 50)))
    c = guard.complete("acme", "m1", "hello", max_output_tokens=100)
    assert c.cost_micros == 100 * 1 + 50 * 2
    assert c.reserved_micros == (5 + 64) * 1 + 100 * 2
    (span,) = spans.get_finished_spans()
    a = dict(span.attributes or {})
    assert a["spend.outcome"] == "ok"
    assert a["spend.cost_micros"] == 200
    assert a["gen_ai.usage.input_tokens"] == 100
    assert a["gen_ai.request.model"] == "m1"
    assert a["spend.tenant"] == "acme"


def test_refusals_never_reach_the_provider(pool: ConnectionPool[Any], clock: Clock) -> None:
    provider = Scripted(ProviderResult("x", 1, 1))
    guard, spans = make(pool, clock, provider)
    with pytest.raises(NoCheckedPrice):
        guard.complete("acme", "unpriced", "hello")
    with pytest.raises(BudgetExceeded):
        guard.complete("acme", "m1", "x" * 600_000, max_output_tokens=10)
    assert provider.calls == 0
    outcomes = [dict(s.attributes or {})["spend.outcome"] for s in spans.get_finished_spans()]
    assert outcomes == ["refused_no_price", "refused_budget"]


def test_rejected_call_is_released(pool: ConnectionPool[Any], clock: Clock) -> None:
    guard, _ = make(pool, clock, Scripted(ProviderRejected("429")))
    with pytest.raises(ProviderRejected):
        guard.complete("acme", "m1", "hello")
    s = guard.ledger.status("acme")[1]
    assert (s.reserved_micros, s.settled_micros) == (0, 0)


@pytest.mark.parametrize("exc", [ProviderFailed("timeout"), RuntimeError("bug")])
def test_failed_call_is_charged_worst_case(
    pool: ConnectionPool[Any], clock: Clock, exc: Exception
) -> None:
    guard, spans = make(pool, clock, Scripted(exc))
    with pytest.raises(type(exc)):
        guard.complete("acme", "m1", "hello", max_output_tokens=100)
    s = guard.ledger.status("acme")[1]
    assert s.reserved_micros == 0
    assert s.settled_micros == (5 + 64) + 200
    assert (
        dict(spans.get_finished_spans()[0].attributes or {})["spend.outcome"] == "provider_failed"
    )
    assert guard.ledger.reconcile(NOW.date()) == []


def test_overrun_is_visible(pool: ConnectionPool[Any], clock: Clock) -> None:
    guard, spans = make(pool, clock, Scripted(ProviderResult("x", 10_000, 10)))
    c = guard.complete("acme", "m1", "hello", max_output_tokens=10)
    assert c.cost_micros > c.reserved_micros
    assert dict(spans.get_finished_spans()[0].attributes or {})["spend.outcome"] == "overrun"


def test_max_output_tokens_is_bounded(pool: ConnectionPool[Any], clock: Clock) -> None:
    guard, _ = make(pool, clock, MockProvider())
    for bad in (0, 5000):
        with pytest.raises(ValueError, match="max_output_tokens"):
            guard.complete("acme", "m1", "hello", max_output_tokens=bad)


def test_recorded_live_probe_replays_within_its_reservations(
    pool: ConnectionPool[Any],
) -> None:
    with pool.connection() as conn:
        seed_prices(conn, ROOT / "evals" / "prices.json")
        conn.execute(
            "INSERT INTO budgets (scope, daily_limit_micros) VALUES ('global', 1000000),"
            " ('tenant:probe', 1000000)"
        )
    checked = dt.datetime(2026, 11, 9, tzinfo=dt.UTC)
    ledger = SpendLedger(pool, clock=lambda: checked)
    provider = ReplayProvider(ROOT / "evals" / "fixtures" / "live-probe.jsonl")
    rows = run_probe(SpendGuard(ledger, provider))
    assert len(rows) == 10
    assert all(r.cost_micros <= r.reserved_micros for r in rows)
    assert all(r.input_tokens <= r.input_bound for r in rows)
    assert ledger.reconcile(checked.date()) == []


# ── HTTP API ───────────────────────────────────────────────────────────────
TOKEN = "test-api"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def api(
    pool: ConnectionPool[Any], clock: Clock, provider: Any, token: str | None = TOKEN
) -> TestClient:
    guard, _ = make(pool, clock, provider)
    return TestClient(create_app(guard, api_token=token))


def test_api_complete_and_budget(pool: ConnectionPool[Any], clock: Clock) -> None:
    c = api(pool, clock, MockProvider())
    r = c.post(
        "/v1/complete",
        headers=AUTH,
        json={"tenant": "acme", "model": "m1", "prompt": "hello", "max_output_tokens": 20},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["cost"] < body["reserved"]
    b = c.get("/v1/budgets/acme", headers=AUTH).json()
    assert [x["scope"] for x in b] == ["global", "tenant:acme"]


@pytest.mark.parametrize(
    ("payload", "provider", "status"),
    [
        ({"model": "unpriced"}, MockProvider(), 409),
        ({"tenant": "small"}, MockProvider(), 402),
        ({}, Scripted(ProviderRejected("quota")), 502),
        ({"tenant": "bad tenant!"}, MockProvider(), 422),
    ],
)
def test_api_refusals(
    pool: ConnectionPool[Any], clock: Clock, payload: dict[str, Any], provider: Any, status: int
) -> None:
    c = api(pool, clock, provider)
    with pool.connection() as conn:
        conn.execute("INSERT INTO budgets (scope, daily_limit_micros) VALUES ('tenant:small', 10)")
    body = {"tenant": "acme", "model": "m1", "prompt": "hi", **payload}
    assert c.post("/v1/complete", headers=AUTH, json=body).status_code == status


def test_api_no_budget_is_403(pool: ConnectionPool[Any], clock: Clock) -> None:
    c = api(pool, clock, MockProvider())
    with pool.connection() as conn:
        conn.execute("DELETE FROM budgets WHERE scope = 'tenant:*'")
    r = c.post("/v1/complete", headers=AUTH, json={"tenant": "acme", "model": "m1", "prompt": "hi"})
    assert r.status_code == 403


def test_api_auth(pool: ConnectionPool[Any], clock: Clock) -> None:
    guard, _ = make(pool, clock, MockProvider())
    c = TestClient(create_app(guard, api_token=TOKEN))
    assert c.post("/v1/complete", json={}).status_code == 401
    assert c.get("/v1/budgets/acme", headers={"Authorization": "Bearer nope"}).status_code == 401
    closed = TestClient(create_app(guard, api_token=None))
    assert closed.get("/v1/budgets/acme").status_code == 503
