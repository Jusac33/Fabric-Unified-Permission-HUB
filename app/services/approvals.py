"""Approval workflow for real (non-dry-run) permission applies.

When ``REQUIRE_APPROVAL`` is enabled, a real apply does not run immediately.
Instead the selected change set is parked as a pending approval request; an
approver must approve it before it can be executed. This adds a human gate in
front of writes to production data estates.
"""
from __future__ import annotations

import json
import uuid
from typing import Optional

from app.services import db


def create_request(*, pairing_id: str, direction: str, row_keys: list[str],
                   requested_by: str = "", note: str = "") -> str:
    db.init_db()
    approval_id = uuid.uuid4().hex[:12]
    db.execute(
        """INSERT INTO approvals
             (id, pairing_id, direction, requested_by, requested_at,
              status, rows_json, note)
           VALUES (?,?,?,?,?,'pending',?,?)""",
        (approval_id, pairing_id, direction, requested_by, db.utcnow(),
         json.dumps(list(row_keys)), note),
    )
    return approval_id


def get(approval_id: str) -> Optional[dict]:
    db.init_db()
    row = db.query_one("SELECT * FROM approvals WHERE id = ?", (approval_id,))
    if row:
        try:
            row["row_keys"] = json.loads(row.get("rows_json") or "[]")
        except Exception:
            row["row_keys"] = []
    return row


def list_requests(status: Optional[str] = None) -> list[dict]:
    db.init_db()
    if status:
        rows = db.query(
            "SELECT * FROM approvals WHERE status = ? ORDER BY requested_at DESC",
            (status,),
        )
    else:
        rows = db.query("SELECT * FROM approvals ORDER BY requested_at DESC LIMIT 100")
    for r in rows:
        try:
            r["row_keys"] = json.loads(r.get("rows_json") or "[]")
        except Exception:
            r["row_keys"] = []
    return rows


def decide(approval_id: str, *, approved: bool, decided_by: str = "") -> bool:
    db.init_db()
    status = "approved" if approved else "rejected"
    cur = db.execute(
        "UPDATE approvals SET status=?, decided_by=?, decided_at=? "
        "WHERE id=? AND status='pending'",
        (status, decided_by, db.utcnow(), approval_id),
    )
    return cur.rowcount > 0


def mark_executed(approval_id: str) -> None:
    db.init_db()
    db.execute("UPDATE approvals SET status='executed' WHERE id=?", (approval_id,))


def pending_count() -> int:
    db.init_db()
    row = db.query_one("SELECT COUNT(*) AS n FROM approvals WHERE status='pending'")
    return int(row["n"]) if row else 0
