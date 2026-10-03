"""SQLite persistence: patients, ingested documents (raw bytes kept for
re-processing) and a hash-chained, tamper-evident audit log of every read and write."""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import threading
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS patients (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS patient_keys (
    key TEXT NOT NULL,
    patient_id TEXT NOT NULL,
    PRIMARY KEY (key, patient_id)
);
CREATE TABLE IF NOT EXISTS documents (
    id TEXT PRIMARY KEY,
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
CREATE INDEX IF NOT EXISTS documents_patient ON documents(patient_id);
CREATE UNIQUE INDEX IF NOT EXISTS documents_dedupe ON documents(patient_id, sha256);
CREATE TABLE IF NOT EXISTS audit (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT NOT NULL UNIQUE,
    ts TEXT NOT NULL,
    event TEXT NOT NULL,
    actor TEXT NOT NULL,
    client_id TEXT,
    patient_id TEXT,
    document_id TEXT,
    detail TEXT NOT NULL,
    prev_hash TEXT NOT NULL,
    hash TEXT NOT NULL
);
"""

GENESIS = "0" * 64


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(8)}"


def canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def sha256(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


class Store:
    def __init__(self, path: str = ":memory:"):
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.lock = threading.RLock()

    # -- generic helpers -------------------------------------------------
    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self.lock:
            return self.conn.execute(sql, params)

    def one(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        with self.lock:
            return self.conn.execute(sql, params).fetchone()

    def all(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self.lock:
            return self.conn.execute(sql, params).fetchall()

    # -- audit log -------------------------------------------------------
    def audit(self, *, ts: str, event: str, actor: str, detail: dict, client_id: str | None = None,
              patient_id: str | None = None, document_id: str | None = None) -> str:
        with self.lock:
            last = self.conn.execute("SELECT hash FROM audit ORDER BY seq DESC LIMIT 1").fetchone()
            prev = last["hash"] if last else GENESIS
            aid = new_id("aud")
            body = canonical({"id": aid, "ts": ts, "event": event, "actor": actor, "client_id": client_id,
                              "patient_id": patient_id, "document_id": document_id, "detail": detail, "prev": prev})
            h = sha256(body)
            self.conn.execute(
                "INSERT INTO audit (id, ts, event, actor, client_id, patient_id, document_id, detail, prev_hash, hash) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (aid, ts, event, actor, client_id, patient_id, document_id, canonical(detail), prev, h),
            )
            return aid

    def audit_entries(self, patient_ids: list[str] | None = None, limit: int = 200) -> list[dict]:
        if patient_ids is None:
            rows = self.all("SELECT * FROM audit ORDER BY seq DESC LIMIT ?", (limit,))
        else:
            marks = ",".join("?" * len(patient_ids))
            rows = self.all(f"SELECT * FROM audit WHERE patient_id IN ({marks}) ORDER BY seq DESC LIMIT ?",
                            (*patient_ids, limit))
        return [self._audit_row(r) for r in rows]

    @staticmethod
    def _audit_row(r: sqlite3.Row) -> dict:
        return {"id": r["id"], "ts": r["ts"], "event": r["event"], "actor": r["actor"], "client_id": r["client_id"],
                "patient_id": r["patient_id"], "document_id": r["document_id"], "detail": json.loads(r["detail"]),
                "hash": r["hash"], "prev_hash": r["prev_hash"]}

    def verify_audit_chain(self) -> dict:
        prev = GENESIS
        count = 0
        for r in self.all("SELECT * FROM audit ORDER BY seq"):
            body = canonical({"id": r["id"], "ts": r["ts"], "event": r["event"], "actor": r["actor"],
                              "client_id": r["client_id"], "patient_id": r["patient_id"], "document_id": r["document_id"],
                              "detail": json.loads(r["detail"]), "prev": prev})
            if r["prev_hash"] != prev or sha256(body) != r["hash"]:
                return {"valid": False, "entries_checked": count, "broken_at": r["id"]}
            prev = r["hash"]
            count += 1
        return {"valid": True, "entries_checked": count, "head": prev}
