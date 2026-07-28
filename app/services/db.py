"""SQLite persistence layer for the Permission Hub.

Uses the Python stdlib ``sqlite3`` (no extra dependency) with a thread-local
connection per thread, WAL mode for concurrent reads, and an idempotent schema
initializer. This is the keystone store for pairings, sync jobs, diff snapshots,
audit events, the identity-reconciliation queue, and approval requests.

Design notes:
- The app is synchronous FastAPI with ThreadPoolExecutor fan-out, so a
  thread-local connection avoids cross-thread sharing issues with sqlite3.
- Rows are returned as ``sqlite3.Row`` (dict-like). Helpers convert to plain dicts.
- ``init_db`` runs at startup and is safe to call repeatedly.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from app.config import settings

_local = threading.local()
_init_lock = threading.Lock()
_initialized = False


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_conn() -> sqlite3.Connection:
    """Return a thread-local SQLite connection (creating it on first use)."""
    conn = getattr(_local, "conn", None)
    if conn is None:
        path: Path = settings.db_path
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path), check_same_thread=False, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        _local.conn = conn
    return conn


_SCHEMA = """
CREATE TABLE IF NOT EXISTS pairings (
    id TEXT PRIMARY KEY,
    label TEXT NOT NULL,
    dbx_workspace_url TEXT NOT NULL,
    uc_catalog TEXT NOT NULL,
    fabric_workspace_id TEXT NOT NULL,
    notes TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sync_jobs (
    id TEXT PRIMARY KEY,
    source_type TEXT NOT NULL,
    config_path TEXT NOT NULL,
    status TEXT NOT NULL,
    dry_run INTEGER NOT NULL DEFAULT 0,
    started_at TEXT,
    finished_at TEXT,
    error TEXT,
    log_tail TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS diff_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pairing_id TEXT NOT NULL,
    taken_at TEXT NOT NULL,
    row_count INTEGER NOT NULL DEFAULT 0,
    dbx_only INTEGER NOT NULL DEFAULT 0,
    fabric_only INTEGER NOT NULL DEFAULT 0,
    in_sync INTEGER NOT NULL DEFAULT 0,
    fingerprint TEXT NOT NULL,
    rows_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_diff_pairing_time
    ON diff_snapshots (pairing_id, taken_at DESC);

CREATE TABLE IF NOT EXISTS audit_events (
    id TEXT PRIMARY KEY,
    timestamp TEXT NOT NULL,
    event TEXT NOT NULL,
    pairing_id TEXT,
    direction TEXT,
    dry_run INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    selected_count INTEGER NOT NULL DEFAULT 0,
    ok_count INTEGER NOT NULL DEFAULT 0,
    skipped_count INTEGER NOT NULL DEFAULT 0,
    failed_count INTEGER NOT NULL DEFAULT 0,
    actor TEXT,
    error TEXT,
    detail_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_time ON audit_events (timestamp DESC);

CREATE TABLE IF NOT EXISTS identity_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pairing_id TEXT,
    principal TEXT NOT NULL,
    principal_type TEXT,
    source_platform TEXT,
    reason TEXT,
    status TEXT NOT NULL DEFAULT 'unresolved',
    resolved_oid TEXT,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    UNIQUE (pairing_id, principal, source_platform)
);

CREATE TABLE IF NOT EXISTS approvals (
    id TEXT PRIMARY KEY,
    pairing_id TEXT NOT NULL,
    direction TEXT NOT NULL,
    requested_by TEXT,
    requested_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    decided_by TEXT,
    decided_at TEXT,
    rows_json TEXT NOT NULL,
    note TEXT
);
CREATE INDEX IF NOT EXISTS idx_approvals_status ON approvals (status, requested_at DESC);
"""


def init_db() -> None:
    """Create tables if needed and run one-time data migrations. Idempotent."""
    global _initialized
    with _init_lock:
        if _initialized:
            return
        conn = get_conn()
        conn.executescript(_SCHEMA)
        conn.commit()
        _migrate_pairings_json(conn)
        _initialized = True


def _migrate_pairings_json(conn: sqlite3.Connection) -> None:
    """One-time import of legacy configs/pairings.json into the DB."""
    row = conn.execute("SELECT COUNT(*) AS n FROM pairings").fetchone()
    if row and row["n"]:
        return  # already populated
    legacy = settings.configs_path / "pairings.json"
    if not legacy.is_file():
        return
    try:
        data = json.loads(legacy.read_text(encoding="utf-8"))
    except Exception:
        return
    if not isinstance(data, list):
        return
    now = _now()
    for entry in data:
        if not isinstance(entry, dict) or not entry.get("id"):
            continue
        conn.execute(
            """INSERT OR IGNORE INTO pairings
               (id, label, dbx_workspace_url, uc_catalog, fabric_workspace_id,
                notes, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (
                entry["id"],
                entry.get("label") or entry.get("uc_catalog") or entry["id"],
                entry.get("dbx_workspace_url", ""),
                entry.get("uc_catalog", ""),
                entry.get("fabric_workspace_id", ""),
                entry.get("notes", ""),
                now,
                now,
            ),
        )
    conn.commit()


# --- generic helpers ---------------------------------------------------------
def query(sql: str, params: Iterable[Any] = ()) -> list[dict]:
    cur = get_conn().execute(sql, tuple(params))
    return [dict(r) for r in cur.fetchall()]


def query_one(sql: str, params: Iterable[Any] = ()) -> Optional[dict]:
    cur = get_conn().execute(sql, tuple(params))
    row = cur.fetchone()
    return dict(row) if row else None


def execute(sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
    conn = get_conn()
    cur = conn.execute(sql, tuple(params))
    conn.commit()
    return cur


def utcnow() -> str:
    return _now()
