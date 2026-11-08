from __future__ import annotations

import datetime as dt
import threading
import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient
from psycopg_pool import ConnectionPool

from conftest import NOW, Clock, setup_prices_and_budgets
from spend_guard import keys
from spend_guard.api import create_app
from spend_guard.errors import BudgetExceeded, RequestIdConflict, RequestInFlight
from spend_guard.gateway import SpendGuard
from spend_guard.ledger import SpendLedger
from spend_guard.providers.base import ProviderFailed, ProviderRejected, ProviderResult
from spend_guard.providers.mock import MockProvider


class Counting:
    """A provider that counts calls and can be made to wait or fail."""

    name = "counting"

    def __init__(self, fail: Exception | None = None, gate: threading.Event | None = None) -> None:
        self.calls = 0
        self.fail = fail
        self.gate = gate
        self._lock = threading.Lock()

    def complete(
        self, model: str, system: str, prompt: str, max_output_tokens: int
    ) -> ProviderResult:
        with self._lock:
            self.calls += 1
        if self.gate is not None:
            self.gate.wait(10)
        if self.fail is not None:
            raise self.fail
        return ProviderResult(f"answer to {prompt}", 100, 50)


def guard(pool: ConnectionPool[Any], clock: Clock, provider: Any, **budgets: Any) -> SpendGuard:
    setup_prices_and_budgets(pool, **budgets)
    return SpendGuard(SpendLedger(pool, clock=clock), provider)


# ── idempotency ─────────────────────────────────────────────────────────────
def test_repeat_returns_the_stored_result_and_charges_once(
    pool: ConnectionPool[Any], clock: Clock
) -> None:
    p = Counting()
    g = guard(pool, clock, p)
    rid = uuid.uuid4()
    first = g.complete("acme", "m1", "hello", request_id=rid)
    again = g.complete("acme", "m1", "hello", request_id=rid)
    assert p.calls == 1
    assert not first.replayed
    assert again.replayed
    assert (again.text, again.cost_micros, again.request_id) == (
        first.text,
        first.cost_micros,
        first.request_id,
    )
    assert g.ledger.status("acme")[1].settled_micros == first.cost_micros
    assert g.ledger.reconcile(NOW.date()) == []


def test_same_id_for_a_different_request_is_a_conflict(
    pool: ConnectionPool[Any], clock: Clock
) -> None:
    g = guard(pool, clock, Counting())
    rid = uuid.uuid4()
    g.complete("acme", "m1", "hello", request_id=rid)
    with pytest.raises(RequestIdConflict, match="different request"):
        g.complete("acme", "m1", "something else", request_id=rid)
    with pytest.raises(RequestIdConflict, match="different request"):
        g.complete("other", "m1", "hello", request_id=rid)  # another tenant cannot read it


def test_concurrent_repeats_make_one_call(pool: ConnectionPool[Any], clock: Clock) -> None:
    gate = threading.Event()
    p = Counting(gate=gate)
    g = guard(pool, clock, p)
    rid = uuid.uuid4()
    outcomes: list[str] = []
    errors: list[BaseException] = []
    barrier = threading.Barrier(8)

    def worker() -> None:
        barrier.wait()
        try:
            g.complete("acme", "m1", "hello", request_id=rid)
            outcomes.append("done")
        except RequestInFlight:
            outcomes.append("in_flight")
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    # Let the losers see "in flight" while the winner's call is held, then release it.
    for _ in range(100):
        if outcomes.count("in_flight") == 7:
            break
        threading.Event().wait(0.05)
    gate.set()
    for t in threads:
        t.join(10)
    assert errors == []
    assert sorted(outcomes) == ["done"] + ["in_flight"] * 7
    assert p.calls == 1
    assert g.complete("acme", "m1", "hello", request_id=rid).replayed
    assert g.ledger.reconcile(NOW.date()) == []


def test_refusal_and_clean_rejection_free_the_id(pool: ConnectionPool[Any], clock: Clock) -> None:
    p = Counting(fail=ProviderRejected("rate limited"))
    g = guard(pool, clock, p, tenants={"small": 10})
    rid = uuid.uuid4()
    with pytest.raises(BudgetExceeded):
        g.complete("small", "m1", "hello", request_id=rid)
    with pool.connection() as conn:
        conn.execute("UPDATE budgets SET daily_limit_micros = 1000000 WHERE scope = 'tenant:small'")
    with pytest.raises(ProviderRejected):
        g.complete("small", "m1", "hello", request_id=rid)  # nothing charged: still free
    p.fail = None
    done = g.complete("small", "m1", "hello", request_id=rid)
    assert not done.replayed
    assert p.calls == 2


def test_a_failure_that_may_be_billed_is_not_retried_under_the_same_id(
    pool: ConnectionPool[Any], clock: Clock
) -> None:
    p = Counting(fail=ProviderFailed("timeout"))
    g = guard(pool, clock, p)
    rid = uuid.uuid4()
    with pytest.raises(ProviderFailed):
        g.complete("acme", "m1", "hello", request_id=rid)
    p.fail = None
    with pytest.raises(RequestIdConflict, match="new request_id"):
        g.complete("acme", "m1", "hello", request_id=rid)
    assert p.calls == 1


def test_abandoned_request_and_prune(pool: ConnectionPool[Any], clock: Clock) -> None:
    g = guard(pool, clock, Counting())
    rid = uuid.uuid4()
    assert g.idempotency.begin(rid, "acme", "fp") is None  # a caller that then crashed
    with pytest.raises(RequestInFlight):
        g.idempotency.begin(rid, "acme", "fp")
    clock.now = NOW + dt.timedelta(minutes=20)
    with pytest.raises(RequestIdConflict, match="abandoned"):
        g.idempotency.begin(rid, "acme", "fp")
    done = uuid.uuid4()
    g.complete("acme", "m1", "hello", request_id=done)
    clock.now = NOW + dt.timedelta(days=2)
    assert g.idempotency.prune(dt.timedelta(hours=24)) == 1


# ── API keys ────────────────────────────────────────────────────────────────
def test_keys_are_stored_as_hashes(pool: ConnectionPool[Any]) -> None:
    with pool.connection() as conn:
        key, key_id = keys.create_key(conn, "acme")
        stored = conn.execute("SELECT key_sha256, tenant FROM api_keys").fetchall()
        assert stored == [(keys.key_hash(key), "acme")]
        assert key.startswith(keys.PREFIX)
        assert key not in str(stored)
        assert keys.tenant_for(conn, key) == "acme"
        assert keys.tenant_for(conn, key + "x") is None
        assert keys.tenant_for(conn, "not-a-key") is None
        assert keys.revoke_key(conn, key_id) == 1
        assert keys.tenant_for(conn, key) is None
        assert keys.revoke_key(conn, key_id) == 0
        with pytest.raises(ValueError, match="hex"):
            keys.revoke_key(conn, "abc")
        with pytest.raises(ValueError, match="tenant"):
            keys.create_key(conn, "*")


def client(pool: ConnectionPool[Any], clock: Clock, token: str | None = None) -> TestClient:
    return TestClient(create_app(guard(pool, clock, MockProvider()), api_token=token))


def bearer(k: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {k}"}


def test_key_decides_the_tenant(pool: ConnectionPool[Any], clock: Clock) -> None:
    c = client(pool, clock)
    with pool.connection() as conn:
        acme, _ = keys.create_key(conn, "acme")
        keys.create_key(conn, "other")
    r = c.post("/v1/complete", headers=bearer(acme), json={"model": "m1", "prompt": "hi"})
    assert r.status_code == 200
    assert r.json()["tenant"] == "acme"
    same = c.post(
        "/v1/complete", headers=bearer(acme), json={"tenant": "acme", "model": "m1", "prompt": "hi"}
    )
    assert same.status_code == 200
    other = c.post(
        "/v1/complete",
        headers=bearer(acme),
        json={"tenant": "other", "model": "m1", "prompt": "hi"},
    )
    assert other.status_code == 403
    assert c.get("/v1/budgets/acme", headers=bearer(acme)).status_code == 200
    assert c.get("/v1/budgets/other", headers=bearer(acme)).status_code == 403
    assert c.get("/v1/budgets/acme", headers=bearer("lsg_wrong")).status_code == 401


def test_revoked_key_and_fail_closed(pool: ConnectionPool[Any], clock: Clock) -> None:
    c = client(pool, clock)
    assert c.get("/v1/budgets/acme", headers=bearer("anything")).status_code == 503
    with pool.connection() as conn:
        key, key_id = keys.create_key(conn, "acme")
    assert c.get("/v1/budgets/acme", headers=bearer(key)).status_code == 200
    with pool.connection() as conn:
        keys.revoke_key(conn, key_id)
    assert c.get("/v1/budgets/acme", headers=bearer(key)).status_code == 503  # no active key


def test_shared_token_still_names_its_tenant(pool: ConnectionPool[Any], clock: Clock) -> None:
    c = client(pool, clock, token="shared-test")
    ok = c.post(
        "/v1/complete",
        headers=bearer("shared-test"),
        json={"tenant": "anyone", "model": "m1", "prompt": "hi"},
    )
    assert ok.status_code == 200
    missing = c.post(
        "/v1/complete", headers=bearer("shared-test"), json={"model": "m1", "prompt": "hi"}
    )
    assert missing.status_code == 422
    star = c.post(
        "/v1/complete",
        headers=bearer("shared-test"),
        json={"tenant": "*", "model": "m1", "prompt": "hi"},
    )
    assert star.status_code == 422


def test_api_request_id_replay_and_conflict(pool: ConnectionPool[Any], clock: Clock) -> None:
    c = client(pool, clock, token="shared-test")
    rid = str(uuid.uuid4())
    body = {"tenant": "acme", "model": "m1", "prompt": "hi", "request_id": rid}
    first = c.post("/v1/complete", headers=bearer("shared-test"), json=body).json()
    again = c.post("/v1/complete", headers=bearer("shared-test"), json=body).json()
    assert first["request_id"] == rid
    assert (first["replayed"], again["replayed"]) == (False, True)
    assert again["reservation_id"] == first["reservation_id"]
    clash = c.post("/v1/complete", headers=bearer("shared-test"), json={**body, "prompt": "x"})
    assert clash.status_code == 409
    bad = c.post(
        "/v1/complete", headers=bearer("shared-test"), json={**body, "request_id": "not-a-uuid"}
    )
    assert bad.status_code == 422
