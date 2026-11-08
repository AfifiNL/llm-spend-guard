"""The price book: every model price is a dated, sourced row. No checked price, no call.

A price counts as checked when a person recorded it with the date they checked it and where
(a vendor pricing page, a contract). The newest row per model wins; rows are never edited, so the
ledger can always point at the exact price a call was charged with. A price older than
`max_age` is treated as missing: prices change, and a stale price is a guess.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any

import psycopg

from spend_guard.errors import NoCheckedPrice


@dataclass(frozen=True)
class Price:
    id: int
    model: str
    input_per_mtok_micros: int
    output_per_mtok_micros: int
    currency: str
    checked_at: dt.datetime
    source: str


def add_price(
    conn: psycopg.Connection[Any],
    model: str,
    input_per_mtok_micros: int,
    output_per_mtok_micros: int,
    *,
    checked_at: dt.datetime,
    source: str,
    currency: str = "USD",
) -> int:
    if checked_at.tzinfo is None:
        raise ValueError("checked_at must be timezone-aware")
    if not source.strip():
        raise ValueError("a price needs a source")
    row = conn.execute(
        "INSERT INTO price_book (model, input_per_mtok_micros, output_per_mtok_micros, currency,"
        " checked_at, source) VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
        (model, input_per_mtok_micros, output_per_mtok_micros, currency, checked_at, source),
    ).fetchone()
    assert row is not None  # noqa: S101 - RETURNING always yields a row
    return int(row[0])


def current_price(
    conn: psycopg.Connection[Any], model: str, *, now: dt.datetime, max_age: dt.timedelta
) -> Price:
    row = conn.execute(
        "SELECT id, model, input_per_mtok_micros, output_per_mtok_micros, currency, checked_at,"
        " source FROM price_book WHERE model = %s AND checked_at <= %s"
        " ORDER BY checked_at DESC, id DESC LIMIT 1",
        (model, now),
    ).fetchone()
    if row is None:
        raise NoCheckedPrice(f"no checked price for model {model!r}: add one to the price book")
    price = Price(*row)
    if now - price.checked_at > max_age:
        raise NoCheckedPrice(
            f"price for {model!r} was checked {price.checked_at:%Y-%m-%d}, more than "
            f"{max_age.days} days ago: check it again"
        )
    return price


def list_prices(conn: psycopg.Connection[Any]) -> list[Price]:
    rows = conn.execute(
        "SELECT DISTINCT ON (model) id, model, input_per_mtok_micros, output_per_mtok_micros,"
        " currency, checked_at, source FROM price_book ORDER BY model, checked_at DESC, id DESC"
    ).fetchall()
    return [Price(*r) for r in rows]
