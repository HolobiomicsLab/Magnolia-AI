"""SQLite control store for the Router prototype.

PROVISIONAL (decision #1): SQLite is an implementation choice, not an
architectural requirement. KGMemory is deliberately NOT this store.

Single writer: only ControlEngine (engine.py) mutates live state through this
store. All multi-step writes happen inside one transaction. Duplicate intake is
rejected by the UNIQUE(source_id, source_event_id) constraint on triggers.
"""

from __future__ import annotations

import json
import os
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
CREATE TABLE IF NOT EXISTS eval_decisions (
    eval_id TEXT PRIMARY KEY,
    text TEXT NOT NULL,
    action TEXT NOT NULL,
    project TEXT,
    work_item_id TEXT,
    confidence REAL NOT NULL,
    reason TEXT NOT NULL,
    evaluator TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS eval_waiters (
    waiter_id TEXT PRIMARY KEY,
    text TEXT NOT NULL,
    context TEXT NOT NULL,
    subscription_key TEXT NOT NULL,
    hold_id TEXT,
    reset_at TEXT,
    route_model TEXT,
    state TEXT NOT NULL,
    owner_pid INTEGER,
    owner_start_token TEXT,
    decision_json TEXT,
    reply_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
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
        self.conn = sqlite3.connect(self.db_path, timeout=5.0)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA busy_timeout = 5000")
        self._tx_depth = 0
        self.conn.executescript(SCHEMA)
        self._migrate_eval_waiters()
        self.conn.commit()

    def _migrate_eval_waiters(self) -> None:
        """Add crash-owner columns to databases created before Plan 3."""
        columns = {
            row[1] for row in self.conn.execute("PRAGMA table_info(eval_waiters)")
        }
        for name, kind in (
            ("owner_pid", "INTEGER"),
            ("owner_start_token", "TEXT"),
            ("decision_json", "TEXT"),
            ("reply_json", "TEXT"),
        ):
            if name not in columns:
                self.conn.execute(f"ALTER TABLE eval_waiters ADD COLUMN {name} {kind}")

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def tx(self):
        """Atomic operation, with real nested-transaction composition.

        The outermost transaction starts IMMEDIATE so cross-process read/check/
        write sequences are serialized. Nested engine/runtime operations use
        savepoints and cannot independently commit their caller's work. Existing
        single-operation callers retain the same public interface.
        """
        depth = self._tx_depth
        savepoint = f"control_sp_{depth}"
        if depth == 0 and not self.conn.in_transaction:
            self.conn.execute("BEGIN IMMEDIATE")
        else:
            # Legacy callers may use insert/update directly before tx(). Join
            # that implicit transaction; the depth-zero exit still commits it.
            self.conn.execute(f"SAVEPOINT {savepoint}")
        self._tx_depth = depth + 1
        try:
            yield self.conn
        except Exception:
            if depth == 0:
                self.conn.rollback()
            else:
                self.conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                self.conn.execute(f"RELEASE SAVEPOINT {savepoint}")
            raise
        else:
            if depth == 0:
                self.conn.commit()
            else:
                self.conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        finally:
            self._tx_depth = depth

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

    def claim_eval_waiter(self, waiter_id: str, now: str) -> dict | None:
        """Atomically claim one pending evaluator continuation exactly once."""
        from campaign_runner.governor import _pid_gone, _verified_process_start_token

        owner_pid = os.getpid()
        owner_token = _verified_process_start_token(owner_pid)
        with self.tx():
            row = self.get("eval_waiters", "waiter_id", waiter_id)
            if row is None:
                return None
            if row["state"] == "running":
                previous_pid = row.get("owner_pid")
                previous_token = row.get("owner_start_token")
                if not isinstance(previous_pid, int) or previous_pid <= 0:
                    return None
                current_token = _verified_process_start_token(previous_pid)
                owner_ended = _pid_gone(previous_pid) or (
                    isinstance(previous_token, str)
                    and current_token is not None
                    and current_token != previous_token
                )
                if not owner_ended:
                    return None
            elif row["state"] != "pending":
                return None
            changed = self.conn.execute(
                "UPDATE eval_waiters SET state = 'running', updated_at = ?, "
                "owner_pid = ?, owner_start_token = ? "
                "WHERE waiter_id = ? AND state IN ('pending', 'running')",
                (now, owner_pid, owner_token, waiter_id),
            )
            return row if changed.rowcount == 1 else None

    def set_eval_waiter_state(
        self,
        waiter_id: str,
        state: str,
        now: str,
        *,
        hold_id: str | None = None,
        reset_at: str | None = None,
        clear_input: bool = False,
        reply_json: str | None = None,
    ) -> None:
        """Persist a waiter transition and optionally erase completed input."""
        changes = {
            "state": state,
            "hold_id": hold_id,
            "reset_at": reset_at,
            "updated_at": now,
            "owner_pid": None,
            "owner_start_token": None,
        }
        if clear_input:
            changes.update({"text": "", "context": ""})
        if reply_json is not None:
            changes["reply_json"] = reply_json
        self.update(
            "eval_waiters",
            "waiter_id",
            waiter_id,
            changes,
        )
        self.conn.commit()


def dumps(obj: dict) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))
