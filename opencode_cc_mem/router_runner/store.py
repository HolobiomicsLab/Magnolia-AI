"""SQLite control store for the Router prototype.

PROVISIONAL (decision #1): SQLite is an implementation choice, not an
architectural requirement. KGMemory is deliberately NOT this store.

Single writer: only ControlEngine (engine.py) mutates live state through this
store. All multi-step writes happen inside one transaction. Duplicate intake is
rejected by the UNIQUE(source_id, source_event_id) constraint on triggers.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS triggers (
    trigger_id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL,
    source_event_id TEXT NOT NULL,
    actor TEXT NOT NULL,
    project_hint TEXT NOT NULL,
    payload TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    received_at TEXT NOT NULL,
    UNIQUE(source_id, source_event_id)
);
CREATE TABLE IF NOT EXISTS decisions (
    decision_id TEXT PRIMARY KEY,
    trigger_id TEXT NOT NULL,
    gate_result TEXT NOT NULL,
    outcome TEXT NOT NULL,
    reason TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS work_items (
    work_item_id TEXT PRIMARY KEY,
    project TEXT NOT NULL,
    trigger_id TEXT NOT NULL,
    mandate_id TEXT,
    state TEXT NOT NULL,
    protocol TEXT NOT NULL,
    protocol_hash TEXT NOT NULL,
    scope TEXT NOT NULL,
    budget TEXT NOT NULL,
    outcome TEXT,
    thread_id TEXT,
    parent_work_item_id TEXT,
    job_run_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS mandates (
    mandate_id TEXT PRIMARY KEY,
    work_item_id TEXT NOT NULL,
    issuer TEXT NOT NULL,
    scope TEXT NOT NULL,
    strength TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    state TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS assignments (
    assignment_id TEXT PRIMARY KEY,
    work_item_id TEXT NOT NULL,
    executor_kind TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    state TEXT NOT NULL,
    owner_pid INTEGER,
    created_at TEXT NOT NULL,
    ended_at TEXT
);
CREATE TABLE IF NOT EXISTS approvals (
    approval_id TEXT PRIMARY KEY,
    work_item_id TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    nonce TEXT NOT NULL,
    approver TEXT,
    state TEXT NOT NULL,
    created_at TEXT NOT NULL,
    approved_at TEXT
);
CREATE TABLE IF NOT EXISTS effects (
    effect_id TEXT PRIMARY KEY,
    work_item_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    payload TEXT NOT NULL,
    state TEXT NOT NULL,
    receipt TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS evidence_links (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    work_item_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    ref_id TEXT NOT NULL,
    path TEXT,
    hash TEXT
);
CREATE TABLE IF NOT EXISTS thread_posts (
    post_id TEXT PRIMARY KEY,
    work_item_id TEXT NOT NULL,
    actor TEXT NOT NULL,
    text TEXT NOT NULL,
    delivered INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS consumed_notices (
    source_event_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    consumed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS findings (
    finding_id TEXT PRIMARY KEY,
    work_item_id TEXT NOT NULL,
    attempt INTEGER,
    kind TEXT NOT NULL,
    outcome_status TEXT NOT NULL,
    text TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


class ControlStore:
    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def tx(self):
        """One transaction per control-engine operation (single writer)."""
        try:
            yield self.conn
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def insert(self, table: str, row: dict) -> None:
        cols = ", ".join(row)
        marks = ", ".join("?" for _ in row)
        self.conn.execute(
            f"INSERT INTO {table} ({cols}) VALUES ({marks})", list(row.values())
        )

    def get(self, table: str, key_col: str, key: str) -> dict | None:
        cur = self.conn.execute(
            f"SELECT * FROM {table} WHERE {key_col} = ?", (key,)
        )
        row = cur.fetchone()
        return dict(row) if row else None

    def find_one(self, table: str, where: str, params: tuple) -> dict | None:
        cur = self.conn.execute(
            f"SELECT * FROM {table} WHERE {where} LIMIT 1", params
        )
        row = cur.fetchone()
        return dict(row) if row else None

    def all(self, table: str, where: str = "1=1", params: tuple = ()) -> list[dict]:
        cur = self.conn.execute(f"SELECT * FROM {table} WHERE {where}", params)
        return [dict(r) for r in cur.fetchall()]

    def update(self, table: str, key_col: str, key: str, changes: dict) -> None:
        sets = ", ".join(f"{c} = ?" for c in changes)
        self.conn.execute(
            f"UPDATE {table} SET {sets} WHERE {key_col} = ?",
            [*changes.values(), key],
        )

    def cas_state(self, work_item_id: str, expected: str, new: str, now: str) -> bool:
        """Compare-and-swap state guard; returns False on concurrent drift."""
        cur = self.conn.execute(
            "UPDATE work_items SET state = ?, updated_at = ? "
            "WHERE work_item_id = ? AND state = ?",
            (new, now, work_item_id, expected),
        )
        return cur.rowcount == 1


def dumps(obj: dict) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))
