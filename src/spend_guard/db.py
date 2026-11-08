"""Connection pool from SPEND_GUARD_DATABASE_URL, or a throwaway local PostgreSQL (pgserver,
installed by `make install`) for the demo when no URL is set."""

from __future__ import annotations

import os
import tempfile
from typing import Any

import psycopg
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
    server = get_server(tempfile.mkdtemp(prefix="spend-guard-pg-"), cleanup_mode="delete")
    return str(server.get_uri())


def make_pool(
    url: str | None = None, *, max_size: int = 10, schema: str | None = None
) -> ConnectionPool[Any]:
    """`schema` puts every table in its own schema (created if missing): the demo and the probe
    use a fresh one, so they never touch the real budgets, prices or ledger in a configured
    database."""
    dsn = url or os.environ.get("SPEND_GUARD_DATABASE_URL") or local_postgres_url()
    kwargs: dict[str, Any] = {"autocommit": True}
    if schema:
        if not schema.replace("_", "").isalnum():
            raise ValueError(f"invalid schema name {schema!r}")
        with psycopg.connect(dsn, autocommit=True) as admin:
            admin.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
        kwargs["options"] = f"-c search_path={schema}"
    pool: ConnectionPool[Any] = ConnectionPool(
        dsn, min_size=1, max_size=max_size, kwargs=kwargs, open=True
    )
    with pool.connection() as conn:
        migrate(conn)
    return pool


def drop_schema(pool: ConnectionPool[Any], schema: str) -> None:
    """Remove a scratch schema made with `make_pool(schema=...)`, ledger included."""
    with pool.connection() as conn:
        conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
