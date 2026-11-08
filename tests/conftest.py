"""Tests run against a real PostgreSQL: SPEND_GUARD_TEST_DATABASE_URL (CI service container),
or a pip-packaged local server (pgserver, `make install`). Each test gets its own schema."""

from __future__ import annotations

import datetime as dt
import os
import tempfile
import uuid
from collections.abc import Iterator
from typing import Any

import psycopg
import pytest
from psycopg_pool import ConnectionPool

from spend_guard.ledger import SpendLedger
from spend_guard.prices import add_price
from spend_guard.schema import migrate

NOW = dt.datetime(2026, 11, 8, 12, 0, tzinfo=dt.UTC)


@pytest.fixture(scope="session")
def database_url() -> str:
    url = os.environ.get("SPEND_GUARD_TEST_DATABASE_URL")
    if url:
        return url
    try:
        import pgserver  # type: ignore[import-untyped]
    except ImportError:
        pytest.fail("no SPEND_GUARD_TEST_DATABASE_URL and no pgserver: run `make install`")
    server = pgserver.get_server(tempfile.mkdtemp(prefix="sg-test-"), cleanup_mode="stop")
    return str(server.get_uri())


@pytest.fixture
def pool(database_url: str) -> Iterator[ConnectionPool[Any]]:
    schema = f"t_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(database_url, autocommit=True) as admin:
        admin.execute(f'CREATE SCHEMA "{schema}"')
    p: ConnectionPool[Any] = ConnectionPool(
        database_url,
        min_size=1,
        max_size=20,
        kwargs={"autocommit": True, "options": f"-c search_path={schema}"},
        open=True,
    )
    with p.connection() as conn:
        migrate(conn)
    yield p
    p.close()
    with psycopg.connect(database_url, autocommit=True) as admin:
        admin.execute(f'DROP SCHEMA "{schema}" CASCADE')


class Clock:
    def __init__(self, now: dt.datetime = NOW) -> None:
        self.now = now

    def __call__(self) -> dt.datetime:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


def setup_prices_and_budgets(
    pool: ConnectionPool[Any],
    *,
    global_limit: int = 1_000_000,
    tenant_default: int | None = 500_000,
    tenants: dict[str, int] | None = None,
    price_in: int = 1_000_000,
    price_out: int = 2_000_000,
) -> None:
    with pool.connection() as conn:
        add_price(
            conn,
            "m1",
            price_in,
            price_out,
            checked_at=NOW - dt.timedelta(days=1),
            source="test fixture",
        )
        conn.execute(
            "INSERT INTO budgets (scope, daily_limit_micros) VALUES ('global', %s)", (global_limit,)
        )
        if tenant_default is not None:
            conn.execute(
                "INSERT INTO budgets (scope, daily_limit_micros) VALUES ('tenant:*', %s)",
                (tenant_default,),
            )
        for t, limit in (tenants or {}).items():
            conn.execute(
                "INSERT INTO budgets (scope, daily_limit_micros) VALUES (%s, %s)",
                (f"tenant:{t}", limit),
            )


@pytest.fixture
def ledger(pool: ConnectionPool[Any], clock: Clock) -> SpendLedger:
    setup_prices_and_budgets(pool)
    return SpendLedger(pool, clock=clock)
