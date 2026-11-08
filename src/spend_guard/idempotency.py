"""Client-supplied request ids: one id, one charge.

A caller that retries (a timeout on its side, a redelivered message) sends the same
`request_id`. The first request with an id claims it (`in_flight`); a second one while the first
runs gets RequestInFlight (HTTP 409); after the first completes, every repeat gets the stored
result back without a new reservation or a new model call.

What frees an id again: a refusal before any call (no price, no budget, budget exceeded) and a
provider that rejected the request before doing any work. Nothing was charged, so retrying with
the same id is safe. What does not: a failure that may have been billed (charged at the worst
case) and an abandoned request (in flight longer than `stale_after`): those ids answer 409 and
the caller must use a new one, so an unknown outcome is never charged twice.

Stored results include the generated text. `prune()` removes finished entries after a while.
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

from spend_guard.errors import RequestIdConflict, RequestInFlight


def fingerprint(tenant: str, model: str, system: str, prompt: str, max_output_tokens: int) -> str:
    blob = json.dumps([tenant, model, system, prompt, max_output_tokens], ensure_ascii=False)
    return hashlib.sha256(blob.encode()).hexdigest()


class IdempotencyStore:
    def __init__(
        self,
        pool: ConnectionPool[Any],
        *,
        clock: Callable[[], dt.datetime],
        stale_after: dt.timedelta = dt.timedelta(minutes=15),
    ) -> None:
        self.pool = pool
        self.clock = clock
        self.stale_after = stale_after

    def begin(self, request_id: uuid.UUID, tenant: str, fp: str) -> dict[str, Any] | None:
        """Claim the id (returns None) or return the stored result of a completed request.
        Raises RequestInFlight or RequestIdConflict otherwise."""
        now = self.clock()
        with self.pool.connection() as conn, conn.transaction():
            claimed = conn.execute(
                "INSERT INTO idempotency (request_id, tenant, fingerprint, status, created_at)"
                " VALUES (%s, %s, %s, 'in_flight', %s) ON CONFLICT (request_id) DO NOTHING"
                " RETURNING request_id",
                (request_id, tenant, fp, now),
            ).fetchone()
            if claimed is not None:
                return None
            row = conn.execute(
                "SELECT tenant, fingerprint, status, result, created_at, note FROM idempotency"
                " WHERE request_id = %s",
                (request_id,),
            ).fetchone()
        if row is None:  # pruned between the two statements: treat as used
            raise RequestIdConflict("request_id was already used; use a new one")
        r_tenant, r_fp, status, result, created_at, note = row
        if r_tenant != tenant or r_fp != fp:
            raise RequestIdConflict("request_id was already used for a different request")
        if status == "completed":
            return dict(result)
        if status == "in_flight":
            if now - created_at > self.stale_after:
                raise RequestIdConflict(
                    "request_id belongs to an abandoned request; use a new request_id"
                )
            raise RequestInFlight("a request with this request_id is still in flight")
        raise RequestIdConflict(
            f"request ended without a result ({note}); it may have been charged, "
            "so retry with a new request_id"
        )

    def complete(
        self, request_id: uuid.UUID, reservation_id: uuid.UUID, result: dict[str, Any]
    ) -> None:
        with self.pool.connection() as conn:
            conn.execute(
                "UPDATE idempotency SET status = 'completed', reservation_id = %s, result = %s,"
                " finished_at = %s WHERE request_id = %s AND status = 'in_flight'",
                (reservation_id, Jsonb(result), self.clock(), request_id),
            )

    def fail(self, request_id: uuid.UUID, note: str) -> None:
        with self.pool.connection() as conn:
            conn.execute(
                "UPDATE idempotency SET status = 'failed', note = %s, finished_at = %s"
                " WHERE request_id = %s AND status = 'in_flight'",
                (note[:200], self.clock(), request_id),
            )

    def release(self, request_id: uuid.UUID) -> None:
        """Nothing was charged: free the id so the caller can retry with it."""
        with self.pool.connection() as conn:
            conn.execute(
                "DELETE FROM idempotency WHERE request_id = %s AND status = 'in_flight'",
                (request_id,),
            )

    def prune(self, older_than: dt.timedelta) -> int:
        cutoff = self.clock() - older_than
        with self.pool.connection() as conn:
            cur = conn.execute(
                "DELETE FROM idempotency WHERE status IN ('completed', 'failed')"
                " AND finished_at < %s",
                (cutoff,),
            )
            return int(cur.rowcount)
