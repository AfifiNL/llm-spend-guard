"""API keys that bind a caller to one tenant.

A key is 32 random bytes, URL-safe, with a recognisable prefix. Only its SHA-256 is stored, so a
database dump does not leak usable keys; the plaintext is shown once, by `create_key`. A key is
identified in listings and for revocation by the first 12 hex characters of its hash.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import re
import secrets
from dataclasses import dataclass
from typing import Any

import psycopg

PREFIX = "lsg_"
TENANT = re.compile(r"^[A-Za-z0-9._-]{1,200}$")


def key_hash(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


@dataclass(frozen=True)
class KeyInfo:
    key_id: str  # first 12 hex characters of the hash
    tenant: str
    created_at: dt.datetime
    revoked_at: dt.datetime | None


def create_key(conn: psycopg.Connection[Any], tenant: str) -> tuple[str, str]:
    """Returns (key, key_id). The key is not stored and cannot be shown again."""
    if not TENANT.fullmatch(tenant) or tenant == "*":
        raise ValueError(f"invalid tenant id {tenant!r}")
    key = PREFIX + secrets.token_urlsafe(32)
    digest = key_hash(key)
    conn.execute("INSERT INTO api_keys (key_sha256, tenant) VALUES (%s, %s)", (digest, tenant))
    return key, digest[:12]


def tenant_for(conn: psycopg.Connection[Any], key: str) -> str | None:
    """The tenant of an active key, or None."""
    if not key.startswith(PREFIX):
        return None
    row = conn.execute(
        "SELECT tenant FROM api_keys WHERE key_sha256 = %s AND revoked_at IS NULL",
        (key_hash(key),),
    ).fetchone()
    return None if row is None else str(row[0])


def revoke_key(conn: psycopg.Connection[Any], key_id: str) -> int:
    """Revoke the key whose hash starts with `key_id` (at least 8 hex characters, must be
    unambiguous). Returns how many keys were revoked (0 or 1)."""
    if not re.fullmatch(r"[0-9a-f]{8,64}", key_id):
        raise ValueError("key id: 8 to 64 lowercase hex characters")
    rows = conn.execute(
        "SELECT key_sha256 FROM api_keys WHERE key_sha256 LIKE %s AND revoked_at IS NULL",
        (key_id + "%",),
    ).fetchall()
    if len(rows) > 1:
        raise ValueError(f"key id {key_id} is ambiguous: give more characters")
    if not rows:
        return 0
    conn.execute("UPDATE api_keys SET revoked_at = now() WHERE key_sha256 = %s", (rows[0][0],))
    return 1


def active_keys(conn: psycopg.Connection[Any]) -> int:
    row = conn.execute("SELECT count(*) FROM api_keys WHERE revoked_at IS NULL").fetchone()
    return int(row[0]) if row else 0


def list_keys(conn: psycopg.Connection[Any]) -> list[KeyInfo]:
    rows = conn.execute(
        "SELECT key_sha256, tenant, created_at, revoked_at FROM api_keys ORDER BY created_at"
    ).fetchall()
    return [KeyInfo(r[0][:12], r[1], r[2], r[3]) for r in rows]
