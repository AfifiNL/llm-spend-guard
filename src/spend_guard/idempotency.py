"""Client-supplied request ids, per tenant: one id, one charge.

A caller that retries (its own timeout, a redelivered message) sends the same `request_id`.
The key is (tenant, request_id): another tenant's ids are separate keys, so an id says nothing
about other tenants. The first request with a key claims it (`in_flight`); a second one while
the first runs gets RequestInFlight; after the first completes, every repeat gets the stored
result back without a new reservation or a new model call.

What frees a key again: a refusal before any call (no price, no budget, budget exceeded) and a
provider that rejected the request before doing any work. Nothing was charged, so retrying with
the same id is safe. What does not: a failure that may have been billed (charged at the worst
case) and an abandoned request (in flight longer than `stale_after`, swept to `failed`). Those
keys answer RequestIdSpent until they are pruned, which by default happens after 30 days, so a
late redelivery inside that window cannot be charged twice.

Stored results include the generated text; completed entries are pruned after 24 hours by
default.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import uuid
from collections.abc import Callable
from typing import Any

from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from spend_guard.errors import RequestIdConflict, RequestIdSpent, RequestInFlight

STALE_AFTER = dt.timedelta(minutes=15)
KEEP_COMPLETED = dt.timedelta(hours=24)
KEEP_FAILED = dt.timedelta(days=30)


def fingerprint(model: str, system: str, prompt: str, max_output_tokens: int) -> str:
    blob = json.dumps([model, system, prompt, max_output_tokens], ensure_ascii=False)
    return hashlib.sha256(blob.encode()).hexdigest()


class IdempotencyStore:
    def __init__(
        self,
        pool: ConnectionPool[Any],
        *,
        clock: Callable[[], dt.datetime],
        stale_after: dt.timedelta = STALE_AFTER,
    ) -> None:
        self.pool = pool
        self.clock = clock
        self.stale_after = stale_after

    def begin(self, tenant: str, request_id: uuid.UUID, fp: str) -> dict[str, Any] | None:
        """Claim the key (returns None) or return the stored result of a completed request.
        Raises RequestInFlight, RequestIdSpent or RequestIdConflict otherwise."""
        now = self.clock()
        with self.pool.connection() as conn, conn.transaction():
            claimed = conn.execute(
                "INSERT INTO idempotency (tenant, request_id, fingerprint, status, created_at)"
                " VALUES (%s, %s, %s, 'in_flight', %s) ON CONFLICT (tenant, request_id)"
                " DO NOTHING RETURNING request_id",
                (tenant, request_id, fp, now),
            ).fetchone()
            if claimed is not None:
                return None
            row = conn.execute(
                "SELECT fingerprint, status, result, created_at, note FROM idempotency"
                " WHERE tenant = %s AND request_id = %s",
                (tenant, request_id),
            ).fetchone()
        if row is None:  # pruned between the two statements
            raise RequestIdSpent("this request_id was used before; use a new one")
        r_fp, status, result, created_at, note = row
        if r_fp != fp:
            raise RequestIdConflict("this request_id was already used for a different request")
        if status == "completed":
            return dict(result)
        if status == "in_flight" and now - created_at <= self.stale_after:
            raise RequestInFlight("a request with this request_id is still in flight")
        reason = note or "abandoned while in flight"
        raise RequestIdSpent(
            f"the request with this request_id ended without a result ({reason}); it may "
            "have been charged, so retry with a new request_id"
        )

    def complete(
        self, tenant: str, request_id: uuid.UUID, reservation_id: uuid.UUID, result: dict[str, Any]
    ) -> None:
        with self.pool.connection() as conn:
            conn.execute(
                "UPDATE idempotency SET status = 'completed', reservation_id = %s, result = %s,"
                " finished_at = %s WHERE tenant = %s AND request_id = %s"
                " AND status = 'in_flight'",
                (reservation_id, Jsonb(result), self.clock(), tenant, request_id),
            )

    def fail(self, tenant: str, request_id: uuid.UUID, note: str) -> None:
        with self.pool.connection() as conn:
            conn.execute(
                "UPDATE idempotency SET status = 'failed', note = %s, finished_at = %s"
                " WHERE tenant = %s AND request_id = %s AND status = 'in_flight'",
                (note[:200], self.clock(), tenant, request_id),
            )

    def release(self, tenant: str, request_id: uuid.UUID) -> None:
        """Nothing was charged: free the key so the caller can retry with it."""
        with self.pool.connection() as conn:
            conn.execute(
                "DELETE FROM idempotency WHERE tenant = %s AND request_id = %s"
                " AND status = 'in_flight'",
                (tenant, request_id),
            )

    def sweep_stale(self) -> int:
        """Mark requests in flight longer than `stale_after` (a crashed caller or a lost
        completion) as failed: they may have been charged and are never run again."""
        now = self.clock()
        with self.pool.connection() as conn:
            cur = conn.execute(
                "UPDATE idempotency SET status = 'failed', note = 'abandoned while in flight',"
                " finished_at = %s WHERE status = 'in_flight' AND created_at < %s",
                (now, now - self.stale_after),
            )
            return int(cur.rowcount)

    def prune(
        self,
        keep_completed: dt.timedelta = KEEP_COMPLETED,
        keep_failed: dt.timedelta = KEEP_FAILED,
    ) -> int:
        """Sweep stale requests, then forget completed ones after `keep_completed` and failed
        ones after `keep_failed` (a redelivery after that window would be processed again)."""
        self.sweep_stale()
        now = self.clock()
        with self.pool.connection() as conn:
            cur = conn.execute(
                "DELETE FROM idempotency WHERE (status = 'completed' AND finished_at < %s)"
                " OR (status = 'failed' AND finished_at < %s)",
                (now - keep_completed, now - keep_failed),
            )
            return int(cur.rowcount)
