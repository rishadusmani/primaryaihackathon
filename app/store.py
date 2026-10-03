"""SQLite store for API keys (hashed) and a local per-key call counter.

Stripe is the source of truth for billing; the local counter exists so the
caller (and a demo) can see usage instantly via GET /usage.
"""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
from contextlib import closing
from datetime import datetime, timezone

KEY_PREFIX = "cn_"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS api_keys (
    key_hash TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL,
    checkout_session_id TEXT UNIQUE,
    created_at TEXT NOT NULL,
    revoked INTEGER NOT NULL DEFAULT 0,
    calls INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS api_keys_customer ON api_keys(customer_id);
"""


def _hash(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


class KeyStore:
    def __init__(self, path: str):
        self.path = path
        with closing(self._conn()) as conn, conn:
            conn.executescript(_SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def create_key(self, customer_id: str, checkout_session_id: str | None = None) -> str | None:
        """Mint a key. Returns None if one was already issued for this checkout session."""
        key = KEY_PREFIX + secrets.token_urlsafe(32)
        try:
            with closing(self._conn()) as conn, conn:
                conn.execute(
                    "INSERT INTO api_keys (key_hash, customer_id, checkout_session_id, created_at)"
                    " VALUES (?, ?, ?, ?)",
                    (_hash(key), customer_id, checkout_session_id,
                     datetime.now(timezone.utc).isoformat()),
                )
        except sqlite3.IntegrityError:
            return None
        return key

    def lookup(self, key: str) -> sqlite3.Row | None:
        with closing(self._conn()) as conn:
            return conn.execute(
                "SELECT * FROM api_keys WHERE key_hash = ? AND revoked = 0", (_hash(key),)
            ).fetchone()

    def record_call(self, key: str) -> int:
        with closing(self._conn()) as conn, conn:
            conn.execute("UPDATE api_keys SET calls = calls + 1 WHERE key_hash = ?", (_hash(key),))
            return conn.execute(
                "SELECT calls FROM api_keys WHERE key_hash = ?", (_hash(key),)
            ).fetchone()["calls"]

    def revoke_customer(self, customer_id: str) -> int:
        with closing(self._conn()) as conn, conn:
            return conn.execute(
                "UPDATE api_keys SET revoked = 1 WHERE customer_id = ?", (customer_id,)
            ).rowcount
