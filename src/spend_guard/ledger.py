"""Reserve-then-settle spend control.

    reserve ── worst-case cost of the call (price x max input and output tokens) is checked
       │       against every budget it touches, under row locks, and held as "reserved"
       ▼
    provider call
       │
       ▼
    settle ─── the actual cost (price x reported usage) replaces the reservation
    release ── the provider refused before doing any work: nothing is charged

Because the check and the hold happen in one transaction that locks the day's counters, two
concurrent calls can never both fit into the same remaining budget. Because the hold is the
worst case, a call that is allowed cannot take a budget past its limit, unless the provider
reports more tokens than the caller's own upper bound (recorded as an overrun, never hidden).

Ledger rows are append-only (enforced by a trigger). `spend_day` holds running totals for fast
checks; `reconcile()` recomputes them from the ledger and reports any difference.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import psycopg
from psycopg_pool import ConnectionPool

from spend_guard.errors import BudgetExceeded, NoBudget, UnknownReservation
from spend_guard.money import cost_micros
from spend_guard.prices import Price, current_price

GLOBAL = "global"
DEFAULT_TENANT = "tenant:*"


def tenant_scope(tenant: str) -> str:
    if not tenant or tenant == "*" or len(tenant) > 200:
        raise ValueError(f"invalid tenant id {tenant!r}")
    return f"tenant:{tenant}"


@dataclass(frozen=True)
class Alert:
    scope: str
    day: dt.date
    level: str  # "warn" (>= warn ratio) | "pause" (a call was refused, or 100% reached)
    spent_micros: int
    limit_micros: int


@dataclass(frozen=True)
class Reservation:
    request_id: uuid.UUID
    tenant: str
    day: dt.date
    model: str
    price: Price
    reserved_micros: int


@dataclass(frozen=True)
class Settlement:
    request_id: uuid.UUID
    cost_micros: int
    reserved_micros: int

    @property
    def overrun(self) -> bool:
        return self.cost_micros > self.reserved_micros


@dataclass(frozen=True)
class ScopeStatus:
    scope: str
    limit_micros: int
    reserved_micros: int
    settled_micros: int

    @property
    def remaining_micros(self) -> int:
        return max(0, self.limit_micros - self.reserved_micros - self.settled_micros)


class SpendLedger:
    def __init__(
        self,
        pool: ConnectionPool[Any],
        *,
        clock: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.UTC),
        max_price_age: dt.timedelta = dt.timedelta(days=31),
        warn_ratio: float = 0.8,
        on_alert: Callable[[Alert], None] | None = None,
    ) -> None:
        if not 0 < warn_ratio < 1:
            raise ValueError("warn_ratio must be between 0 and 1")
        self.pool = pool
        self.clock = clock
        self.max_price_age = max_price_age
        self.warn_ratio = warn_ratio
        self.on_alert = on_alert

    # ── helpers ──────────────────────────────────────────────────────────────
    @staticmethod
    def _limit(conn: psycopg.Connection[Any], scope: str) -> int | None:
        row = conn.execute(
            "SELECT daily_limit_micros FROM budgets WHERE scope = %s", (scope,)
        ).fetchone()
        return None if row is None else int(row[0])

    def _limits(self, conn: psycopg.Connection[Any], tenant: str) -> dict[str, int]:
        g = self._limit(conn, GLOBAL)
        if g is None:
            raise NoBudget("no global daily budget configured")
        scope = tenant_scope(tenant)
        t = self._limit(conn, scope)
        if t is None:
            t = self._limit(conn, DEFAULT_TENANT)
        if t is None:
            raise NoBudget(f"no budget for {scope} and no default tenant budget")
        return {GLOBAL: g, scope: t}

    @staticmethod
    def _lock_counters(
        conn: psycopg.Connection[Any], scopes: list[str], day: dt.date
    ) -> dict[str, tuple[int, int]]:
        scopes = sorted(scopes)  # one lock order everywhere: no deadlocks
        for scope in scopes:
            conn.execute(
                "INSERT INTO spend_day (scope, day) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                (scope, day),
            )
        rows = conn.execute(
            "SELECT scope, reserved_micros, settled_micros FROM spend_day"
            " WHERE day = %s AND scope = ANY(%s) ORDER BY scope FOR UPDATE",
            (day, scopes),
        ).fetchall()
        return {r[0]: (int(r[1]), int(r[2])) for r in rows}

    @staticmethod
    def _alert(conn: psycopg.Connection[Any], alert: Alert) -> bool:
        row = conn.execute(
            "INSERT INTO alerts (scope, day, level, spent_micros, limit_micros)"
            " VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING RETURNING scope",
            (alert.scope, alert.day, alert.level, alert.spent_micros, alert.limit_micros),
        ).fetchone()
        return row is not None

    def _thresholds(
        self,
        conn: psycopg.Connection[Any],
        day: dt.date,
        limits: dict[str, int],
        totals: dict[str, int],
    ) -> list[Alert]:
        raised: list[Alert] = []
        for scope in sorted(limits):
            limit, spent = limits[scope], totals[scope]
            for level, threshold in (("warn", self.warn_ratio * limit), ("pause", limit)):
                if limit > 0 and spent >= threshold:
                    a = Alert(scope, day, level, spent, limit)
                    if self._alert(conn, a):
                        raised.append(a)
        return raised

    def _notify(self, alerts: list[Alert]) -> None:
        if self.on_alert:
            for a in alerts:
                self.on_alert(a)

    # ── public API ───────────────────────────────────────────────────────────
    def reserve(
        self,
        tenant: str,
        model: str,
        *,
        max_input_tokens: int,
        max_output_tokens: int,
        request_id: uuid.UUID | None = None,
    ) -> Reservation:
        if max_input_tokens < 0 or max_output_tokens <= 0:
            raise ValueError("token bounds must be positive")
        now = self.clock()
        day = now.astimezone(dt.UTC).date()
        rid = request_id or uuid.uuid4()
        refused: BudgetExceeded | None = None
        alerts: list[Alert] = []
        with self.pool.connection() as conn, conn.transaction():
            price = current_price(conn, model, now=now, max_age=self.max_price_age)
            amount = cost_micros(
                max_input_tokens,
                max_output_tokens,
                price.input_per_mtok_micros,
                price.output_per_mtok_micros,
            )
            limits = self._limits(conn, tenant)
            counters = self._lock_counters(conn, list(limits), day)
            for scope in sorted(limits):
                reserved, settled = counters[scope]
                if reserved + settled + amount > limits[scope]:
                    refused = BudgetExceeded(scope, reserved + settled, amount, limits[scope])
                    a = Alert(scope, day, "pause", reserved + settled, limits[scope])
                    if self._alert(conn, a):
                        alerts.append(a)
                    break
            if refused is None:
                conn.execute(
                    "INSERT INTO reservations (request_id, tenant, day, model, price_id,"
                    " budget_scopes, reserved_micros, created_at, state)"
                    " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'open')",
                    (rid, tenant, day, model, price.id, sorted(limits), amount, now),
                )
                conn.execute(
                    "INSERT INTO ledger (request_id, tenant, day, model, price_id, kind,"
                    " amount_micros) VALUES (%s, %s, %s, %s, %s, 'reserve', %s)",
                    (rid, tenant, day, model, price.id, amount),
                )
                conn.execute(
                    "UPDATE spend_day SET reserved_micros = reserved_micros + %s"
                    " WHERE day = %s AND scope = ANY(%s)",
                    (amount, day, sorted(limits)),
                )
                totals = {s: counters[s][0] + counters[s][1] + amount for s in limits}
                alerts += [
                    a for a in self._thresholds(conn, day, limits, totals) if a.level == "warn"
                ]
        self._notify(alerts)
        if refused is not None:
            raise refused
        return Reservation(rid, tenant, day, model, price, amount)

    def _close(
        self,
        request_id: uuid.UUID,
        *,
        kind: str,
        cost: Callable[[Price, int], int] | None,
        input_tokens: int | None,
        output_tokens: int | None,
        note: str,
    ) -> Settlement:
        now = self.clock()
        alerts: list[Alert] = []
        with self.pool.connection() as conn, conn.transaction():
            row = conn.execute(
                "SELECT tenant, day, model, price_id, budget_scopes, reserved_micros, state"
                " FROM reservations WHERE request_id = %s FOR UPDATE",
                (request_id,),
            ).fetchone()
            if row is None or row[6] != "open":
                state = "missing" if row is None else row[6]
                raise UnknownReservation(f"reservation {request_id} is {state}")
            tenant, day, model, price_id, scopes, reserved = row[:6]
            prow = conn.execute(
                "SELECT id, model, input_per_mtok_micros, output_per_mtok_micros, currency,"
                " checked_at, source FROM price_book WHERE id = %s",
                (price_id,),
            ).fetchone()
            assert prow is not None  # noqa: S101 - foreign key guarantees it
            charged = cost(Price(*prow), int(reserved)) if cost else 0
            counters = self._lock_counters(conn, list(scopes), day)
            conn.execute(
                "UPDATE spend_day SET reserved_micros = reserved_micros - %s,"
                " settled_micros = settled_micros + %s WHERE day = %s AND scope = ANY(%s)",
                (reserved, charged, day, list(scopes)),
            )
            conn.execute(
                "UPDATE reservations SET state = %s, closed_at = %s WHERE request_id = %s",
                ("released" if kind == "release" else "settled", now, request_id),
            )
            if charged > reserved:
                note = (note + "; " if note else "") + f"overrun: charged {charged} > {reserved}"
            conn.execute(
                "INSERT INTO ledger (request_id, tenant, day, model, price_id, kind,"
                " amount_micros, released_micros, input_tokens, output_tokens, note)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    request_id,
                    tenant,
                    day,
                    model,
                    price_id,
                    kind,
                    charged,
                    reserved,
                    input_tokens,
                    output_tokens,
                    note,
                ),
            )
            if charged:
                limits = {s: self._limit(conn, s) for s in scopes}
                if limits.get(f"tenant:{tenant}") is None:
                    limits[f"tenant:{tenant}"] = self._limit(conn, DEFAULT_TENANT)
                known = {s: v for s, v in limits.items() if v is not None}
                totals = {
                    s: counters[s][0] - int(reserved) + counters[s][1] + charged for s in known
                }
                alerts = self._thresholds(conn, day, known, totals)
        self._notify(alerts)
        return Settlement(request_id, charged, int(reserved))

    def settle(
        self, request_id: uuid.UUID, input_tokens: int, output_tokens: int, note: str = ""
    ) -> Settlement:
        """Charge the actual cost from the provider's reported usage."""
        if input_tokens < 0 or output_tokens < 0:
            raise ValueError("usage must be non-negative")
        return self._close(
            request_id,
            kind="settle",
            cost=lambda p, _: cost_micros(
                input_tokens, output_tokens, p.input_per_mtok_micros, p.output_per_mtok_micros
            ),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            note=note,
        )

    def settle_worst_case(self, request_id: uuid.UUID, note: str) -> Settlement:
        """Usage is unknown (timeout, crash, a server error after the request was sent):
        charge the full reservation. Over-counting is the safe direction for a budget."""
        return self._close(
            request_id,
            kind="settle",
            cost=lambda _, r: r,
            input_tokens=None,
            output_tokens=None,
            note=note,
        )

    def release(self, request_id: uuid.UUID, note: str = "") -> Settlement:
        """The provider refused before doing any work: nothing is charged."""
        return self._close(
            request_id,
            kind="release",
            cost=None,
            input_tokens=None,
            output_tokens=None,
            note=note,
        )

    def sweep(self, older_than: dt.timedelta) -> int:
        """Charge reservations left open longer than `older_than` (a crashed caller) at their
        worst case. Returns how many were closed."""
        cutoff = self.clock() - older_than
        with self.pool.connection() as conn:
            ids = [
                r[0]
                for r in conn.execute(
                    "SELECT request_id FROM reservations WHERE state = 'open' AND created_at < %s"
                    " ORDER BY created_at",
                    (cutoff,),
                ).fetchall()
            ]
        closed = 0
        for rid in ids:
            try:
                self.settle_worst_case(rid, "swept: no settlement in time")
                closed += 1
            except UnknownReservation:
                continue  # settled concurrently
        return closed

    def status(self, tenant: str, day: dt.date | None = None) -> list[ScopeStatus]:
        d = day or self.clock().astimezone(dt.UTC).date()
        with self.pool.connection() as conn:
            limits = self._limits(conn, tenant)
            out = []
            for scope in sorted(limits):
                row = conn.execute(
                    "SELECT reserved_micros, settled_micros FROM spend_day"
                    " WHERE scope = %s AND day = %s",
                    (scope, d),
                ).fetchone()
                reserved, settled = (int(row[0]), int(row[1])) if row else (0, 0)
                out.append(ScopeStatus(scope, limits[scope], reserved, settled))
            return out

    def reconcile(self, day: dt.date) -> list[str]:
        """Recompute every counter for `day` from the ledger. Returns the differences."""
        with self.pool.connection() as conn:
            rows = conn.execute(
                "SELECT tenant,"
                " sum(CASE WHEN kind = 'reserve' THEN amount_micros ELSE 0 END)"
                "   - sum(released_micros),"
                " sum(CASE WHEN kind = 'settle' THEN amount_micros ELSE 0 END)"
                " FROM ledger WHERE day = %s GROUP BY tenant",
                (day,),
            ).fetchall()
            expected: dict[str, tuple[int, int]] = {}
            g_res = g_set = 0
            for tenant, res, settled in rows:
                expected[f"tenant:{tenant}"] = (int(res), int(settled))
                g_res += int(res)
                g_set += int(settled)
            if rows:
                expected[GLOBAL] = (g_res, g_set)
            actual = {
                r[0]: (int(r[1]), int(r[2]))
                for r in conn.execute(
                    "SELECT scope, reserved_micros, settled_micros FROM spend_day WHERE day = %s",
                    (day,),
                ).fetchall()
            }
        problems = []
        for scope in sorted(set(expected) | set(actual)):
            if expected.get(scope, (0, 0)) != actual.get(scope, (0, 0)):
                problems.append(
                    f"{scope}: ledger says {expected.get(scope)}, counters say {actual.get(scope)}"
                )
        return problems

    def alerts(self, day: dt.date | None = None) -> list[Alert]:
        d = day or self.clock().astimezone(dt.UTC).date()
        with self.pool.connection() as conn:
            rows = conn.execute(
                "SELECT scope, day, level, spent_micros, limit_micros FROM alerts"
                " WHERE day = %s ORDER BY at, scope, level",
                (d,),
            ).fetchall()
        return [Alert(r[0], r[1], r[2], int(r[3]), int(r[4])) for r in rows]
