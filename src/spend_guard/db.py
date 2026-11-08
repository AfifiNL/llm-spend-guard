"""Connection pool from SPEND_GUARD_DATABASE_URL, or a throwaway local PostgreSQL (pgserver,
installed by `make install`) for the demo when no URL is set."""

from __future__ import annotations

import os
import tempfile
from typing import Any

from psycopg_pool import ConnectionPool

from spend_guard.schema import migrate


def local_postgres_url() -> str:  # pragma: no cover - exercised by the demo and test setup
    try:
        import pgserver
    except ImportError as exc:
        raise RuntimeError(
            "set SPEND_GUARD_DATABASE_URL, or run `make install` for the local PostgreSQL"
        ) from exc
    get_server = pgserver.get_server  # type: ignore[attr-defined]
    server = get_server(tempfile.mkdtemp(prefix="spend-guard-pg-"), cleanup_mode="stop")
    return str(server.get_uri())


def make_pool(url: str | None = None, *, max_size: int = 10) -> ConnectionPool[Any]:
    dsn = url or os.environ.get("SPEND_GUARD_DATABASE_URL") or local_postgres_url()
    pool: ConnectionPool[Any] = ConnectionPool(
        dsn, min_size=1, max_size=max_size, kwargs={"autocommit": True}, open=True
    )
    with pool.connection() as conn:
        migrate(conn)
    return pool
