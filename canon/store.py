"""Persistence: accounts, API keys, usage, patients, documents, a
hash-chained (tamper-evident) audit log, and per-request API metering
(api_requests, behind the customer usage dashboard).

Two backends behind one interface:
* SQLite: local development, tests, single-node deployments (default).
* Postgres: hosted deployments (Supabase). Selected when DATABASE_URL is set.
  Tables live in the `canon` schema (see migrations/001_init.sql), which is
  not exposed through Supabase's REST API.

SQL in the app is written once with `?` placeholders and `ON CONFLICT`, which
both engines understand; the Postgres backend rewrites placeholders.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import secrets
import sqlite3
import threading
from typing import Any, Iterator

SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    email TEXT,
    status TEXT NOT NULL,
    stripe_customer_id TEXT UNIQUE,
    stripe_subscription_id TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS api_keys (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    key_hash TEXT NOT NULL UNIQUE,
    prefix TEXT NOT NULL,
    created_at TEXT NOT NULL,
    revoked_at TEXT
);
CREATE TABLE IF NOT EXISTS usage_events (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    billable INTEGER NOT NULL DEFAULT 1,
    document_id TEXT,
    created_at TEXT NOT NULL,
    reported_at TEXT,
    report_error TEXT
);
CREATE INDEX IF NOT EXISTS usage_account ON usage_events(account_id, created_at);
CREATE TABLE IF NOT EXISTS stripe_events (
    id TEXT PRIMARY KEY,
    type TEXT NOT NULL,
    received_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS patients (
    account_id TEXT NOT NULL,
    id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (account_id, id)
);
CREATE TABLE IF NOT EXISTS patient_keys (
    account_id TEXT NOT NULL,
    key TEXT NOT NULL,
    patient_id TEXT NOT NULL,
    PRIMARY KEY (account_id, key, patient_id)
);
CREATE TABLE IF NOT EXISTS documents (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    patient_id TEXT NOT NULL,
    format TEXT NOT NULL,
    filename TEXT,
    source_name TEXT,
    received_at TEXT NOT NULL,
    document_date TEXT,
    sha256 TEXT NOT NULL,
    size INTEGER NOT NULL,
    raw BLOB NOT NULL,
    info TEXT NOT NULL,
    items TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS documents_patient ON documents(account_id, patient_id);
CREATE UNIQUE INDEX IF NOT EXISTS documents_dedupe ON documents(account_id, patient_id, sha256);
CREATE TABLE IF NOT EXISTS audit (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT NOT NULL UNIQUE,
    ts TEXT NOT NULL,
    event TEXT NOT NULL,
    actor TEXT NOT NULL,
    account_id TEXT,
    patient_id TEXT,
    document_id TEXT,
    detail TEXT NOT NULL,
    prev_hash TEXT NOT NULL,
    hash TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS audit_account ON audit(account_id, seq);
CREATE TABLE IF NOT EXISTS api_requests (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    account_id TEXT NOT NULL,
    channel TEXT NOT NULL,
    operation TEXT NOT NULL,
    status INTEGER NOT NULL,
    latency_ms REAL NOT NULL,
    bytes_in INTEGER NOT NULL DEFAULT 0,
    bytes_out INTEGER NOT NULL DEFAULT 0,
    patient_id TEXT,
    llm_input_tokens INTEGER NOT NULL DEFAULT 0,
    llm_output_tokens INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS api_requests_account ON api_requests(account_id, ts);
"""

GENESIS = "0" * 64


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(8)}"


def canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def sha256(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


# ---------------------------------------------------------------------------- backends
class _SQLite:
    dialect = "sqlite"

    def __init__(self, path: str):
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SQLITE_SCHEMA)
        self.lock = threading.RLock()

    def execute(self, sql: str, params: tuple = ()) -> int:
        with self.lock:
            return self.conn.execute(sql, params).rowcount

    def one(self, sql: str, params: tuple = ()):
        with self.lock:
            return self.conn.execute(sql, params).fetchone()

    def all(self, sql: str, params: tuple = ()):
        with self.lock:
            return self.conn.execute(sql, params).fetchall()

    @contextlib.contextmanager
    def serialized(self, name: str) -> Iterator[None]:
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield
                self.conn.execute("COMMIT")
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise


class _Postgres:
    dialect = "postgres"

    def __init__(self, url: str):
        import psycopg
        from psycopg.rows import dict_row

        self._psycopg = psycopg
        self._connect = lambda: psycopg.connect(
            url, autocommit=True, row_factory=dict_row, prepare_threshold=None,  # pooler-safe
            options="-c search_path=canon")
        self.conn = self._connect()
        self.lock = threading.RLock()
        self._in_tx = False

    @staticmethod
    def _sql(sql: str) -> str:
        return sql.replace("?", "%s")

    def _run(self, fn):
        try:
            return fn()
        except self._psycopg.OperationalError:
            if self._in_tx:
                raise
            self.conn = self._connect()  # serverless connections go stale; retry once
            return fn()

    def execute(self, sql: str, params: tuple = ()) -> int:
        with self.lock:
            return self._run(lambda: self.conn.execute(self._sql(sql), params).rowcount)

    def one(self, sql: str, params: tuple = ()):
        with self.lock:
            return self._run(lambda: self.conn.execute(self._sql(sql), params).fetchone())

    def all(self, sql: str, params: tuple = ()):
        with self.lock:
            return self._run(lambda: self.conn.execute(self._sql(sql), params).fetchall())

    @contextlib.contextmanager
    def serialized(self, name: str) -> Iterator[None]:
        """Transaction + advisory lock: serializes writers across serverless instances."""
        with self.lock:
            with self.conn.transaction():
                self._in_tx = True
                try:
                    self.conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (name,))
                    yield
                finally:
                    self._in_tx = False


# ---------------------------------------------------------------------------- store
class Store:
    def __init__(self, path: str | None = None):
        url = os.environ.get("DATABASE_URL") if path is None else None
        if url or (path or "").startswith("postgres"):
            self.db = _Postgres(url or path)
        else:
            self.db = _SQLite(path or ":memory:")

    @property
    def dialect(self) -> str:
        return self.db.dialect

    def execute(self, sql: str, params: tuple = ()) -> int:
        return self.db.execute(sql, params)

    def one(self, sql: str, params: tuple = ()):
        return self.db.one(sql, params)

    def all(self, sql: str, params: tuple = ()):
        return self.db.all(sql, params)

    def serialized(self, name: str):
        return self.db.serialized(name)

    # -- audit log -------------------------------------------------------
    def audit(self, *, ts: str, event: str, actor: str, detail: dict, account_id: str | None = None,
              patient_id: str | None = None, document_id: str | None = None) -> str:
        with self.serialized("canon_audit"):
            last = self.one("SELECT hash FROM audit ORDER BY seq DESC LIMIT 1")
            prev = last["hash"] if last else GENESIS
            aid = new_id("aud")
            body = canonical({"id": aid, "ts": ts, "event": event, "actor": actor, "account_id": account_id,
                              "patient_id": patient_id, "document_id": document_id, "detail": detail,
                              "prev": prev})
            h = sha256(body)
            self.execute(
                "INSERT INTO audit (id, ts, event, actor, account_id, patient_id, document_id, detail, prev_hash, "
                "hash) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (aid, ts, event, actor, account_id, patient_id, document_id, canonical(detail), prev, h),
            )
            return aid

    def audit_entries(self, account_id: str, patient_id: str | None = None, limit: int = 200) -> list[dict]:
        if patient_id:
            rows = self.all("SELECT * FROM audit WHERE account_id=? AND patient_id=? ORDER BY seq DESC LIMIT ?",
                            (account_id, patient_id, limit))
        else:
            rows = self.all("SELECT * FROM audit WHERE account_id=? ORDER BY seq DESC LIMIT ?", (account_id, limit))
        return [self._audit_row(r) for r in rows]

    @staticmethod
    def _audit_row(r) -> dict:
        return {"id": r["id"], "ts": r["ts"], "event": r["event"], "actor": r["actor"],
                "patient_id": r["patient_id"], "document_id": r["document_id"], "detail": json.loads(r["detail"]),
                "hash": r["hash"], "prev_hash": r["prev_hash"]}

    def verify_audit_chain(self) -> dict:
        prev = GENESIS
        count = 0
        for r in self.all("SELECT * FROM audit ORDER BY seq"):
            body = canonical({"id": r["id"], "ts": r["ts"], "event": r["event"], "actor": r["actor"],
                              "account_id": r["account_id"], "patient_id": r["patient_id"],
                              "document_id": r["document_id"], "detail": json.loads(r["detail"]), "prev": prev})
            if r["prev_hash"] != prev or sha256(body) != r["hash"]:
                return {"valid": False, "entries_checked": count, "broken_at": r["id"]}
            prev = r["hash"]
            count += 1
        return {"valid": True, "entries_checked": count, "head": prev}
