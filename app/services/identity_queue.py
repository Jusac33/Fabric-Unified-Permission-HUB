"""Identity reconciliation queue.

When the diff cannot confidently map a Databricks principal to an Entra object
(nested groups, SCIM-only groups, service principals named differently, etc.),
it is parked here so an operator can resolve it instead of the permission being
silently dropped or mis-applied.
"""
from __future__ import annotations

from typing import Optional

from app.services import db

STATUSES = ("unresolved", "resolved", "ignored")


def enqueue(*, pairing_id: Optional[str], principal: str,
            principal_type: str = "", source_platform: str = "unity_catalog",
            reason: str = "") -> None:
    """Record (or refresh) an unmapped principal. Idempotent per principal."""
    if not principal:
        return
    db.init_db()
    now = db.utcnow()
    db.execute(
        """INSERT INTO identity_queue
             (pairing_id, principal, principal_type, source_platform, reason,
              status, first_seen, last_seen)
           VALUES (?,?,?,?,?,'unresolved',?,?)
           ON CONFLICT(pairing_id, principal, source_platform) DO UPDATE SET
             last_seen=excluded.last_seen,
             reason=excluded.reason,
             principal_type=excluded.principal_type""",
        (pairing_id, principal, principal_type, source_platform, reason, now, now),
    )


def list_items(status: Optional[str] = None,
               pairing_id: Optional[str] = None) -> list[dict]:
    db.init_db()
    sql = "SELECT * FROM identity_queue"
    conds, params = [], []
    if status:
        conds.append("status = ?")
        params.append(status)
    if pairing_id:
        conds.append("pairing_id = ?")
        params.append(pairing_id)
    if conds:
        sql += " WHERE " + " AND ".join(conds)
    sql += " ORDER BY last_seen DESC, id DESC"
    return db.query(sql, params)


def resolve(item_id: int, resolved_oid: str) -> bool:
    db.init_db()
    cur = db.execute(
        "UPDATE identity_queue SET status='resolved', resolved_oid=? WHERE id=?",
        (resolved_oid, item_id),
    )
    return cur.rowcount > 0


def ignore(item_id: int) -> bool:
    db.init_db()
    cur = db.execute(
        "UPDATE identity_queue SET status='ignored' WHERE id=?", (item_id,)
    )
    return cur.rowcount > 0


def counts() -> dict:
    db.init_db()
    rows = db.query(
        "SELECT status, COUNT(*) AS n FROM identity_queue GROUP BY status"
    )
    out = {s: 0 for s in STATUSES}
    for r in rows:
        out[r["status"]] = r["n"]
    return out
