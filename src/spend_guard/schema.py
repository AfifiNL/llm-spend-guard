"""PostgreSQL schema (PostgreSQL 14 or newer). `migrate` is idempotent and safe to run on every
start.

The triggers stop accidental edits, not a hostile table owner: the owner can disable a trigger.
Run the service under a role that has only SELECT and INSERT on `ledger` and `price_book`
(see docs/DESIGN.md)."""

from __future__ import annotations

from typing import Any

import psycopg

SCHEMA = """
CREATE TABLE IF NOT EXISTS price_book (
    id                      BIGSERIAL PRIMARY KEY,
    model                   TEXT NOT NULL,
    input_per_mtok_micros   BIGINT NOT NULL CHECK (input_per_mtok_micros >= 0),
    output_per_mtok_micros  BIGINT NOT NULL CHECK (output_per_mtok_micros >= 0),
    currency                TEXT NOT NULL DEFAULT 'USD',
    checked_at              TIMESTAMPTZ NOT NULL,
    source                  TEXT NOT NULL CHECK (length(trim(source)) > 0),
    recorded_at             TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS price_book_model ON price_book (model, checked_at DESC, id DESC);

CREATE TABLE IF NOT EXISTS budgets (
    scope               TEXT PRIMARY KEY CHECK (scope = 'global' OR scope LIKE 'tenant:_%'),
    daily_limit_micros  BIGINT NOT NULL CHECK (daily_limit_micros >= 0),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Running totals per scope and UTC day. A cache of the ledger, updated in the same
-- transaction; `reconcile()` proves it matches.
CREATE TABLE IF NOT EXISTS spend_day (
    scope            TEXT NOT NULL,
    day              DATE NOT NULL,
    reserved_micros  BIGINT NOT NULL DEFAULT 0 CHECK (reserved_micros >= 0),
    settled_micros   BIGINT NOT NULL DEFAULT 0 CHECK (settled_micros >= 0),
    PRIMARY KEY (scope, day)
);

CREATE TABLE IF NOT EXISTS reservations (
    request_id       UUID PRIMARY KEY,
    tenant           TEXT NOT NULL,
    day              DATE NOT NULL,
    model            TEXT NOT NULL,
    price_id         BIGINT NOT NULL REFERENCES price_book(id),
    budget_scopes    TEXT[] NOT NULL,
    reserved_micros  BIGINT NOT NULL CHECK (reserved_micros >= 0),
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    state            TEXT NOT NULL CHECK (state IN ('open', 'settled', 'released', 'swept')),
    closed_at        TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS reservations_open ON reservations (created_at) WHERE state = 'open';

CREATE TABLE IF NOT EXISTS ledger (
    id             BIGSERIAL PRIMARY KEY,
    at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    request_id     UUID NOT NULL,
    tenant         TEXT NOT NULL,
    day            DATE NOT NULL,
    model          TEXT NOT NULL,
    price_id       BIGINT NOT NULL REFERENCES price_book(id),
    kind           TEXT NOT NULL CHECK (kind IN ('reserve', 'settle', 'release')),
    amount_micros  BIGINT NOT NULL CHECK (amount_micros >= 0),
    released_micros BIGINT NOT NULL DEFAULT 0 CHECK (released_micros >= 0),
    input_tokens   BIGINT,
    output_tokens  BIGINT,
    note           TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ledger_request ON ledger (request_id);

CREATE OR REPLACE FUNCTION ledger_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION '% is append-only', TG_TABLE_NAME;
END
$$;
-- Prices are never edited: a new price is a new row, so settlement and history stay exact.
CREATE OR REPLACE TRIGGER price_book_no_update_delete BEFORE UPDATE OR DELETE ON price_book
    FOR EACH ROW EXECUTE FUNCTION ledger_append_only();
CREATE OR REPLACE TRIGGER ledger_no_update_delete BEFORE UPDATE OR DELETE ON ledger
    FOR EACH ROW EXECUTE FUNCTION ledger_append_only();
CREATE OR REPLACE TRIGGER ledger_no_truncate BEFORE TRUNCATE ON ledger
    FOR EACH STATEMENT EXECUTE FUNCTION ledger_append_only();

CREATE TABLE IF NOT EXISTS alerts (
    scope          TEXT NOT NULL,
    day            DATE NOT NULL,
    level          TEXT NOT NULL CHECK (level IN ('warn', 'pause')),
    at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    spent_micros   BIGINT NOT NULL,
    limit_micros   BIGINT NOT NULL,
    PRIMARY KEY (scope, day, level)
);
"""


def migrate(conn: psycopg.Connection[Any]) -> None:
    with conn.transaction():
        # Serialise concurrent migrations (several workers starting at once).
        conn.execute("SELECT pg_advisory_xact_lock(7313001)")
        conn.execute(SCHEMA)
