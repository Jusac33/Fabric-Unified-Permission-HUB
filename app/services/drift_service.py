"""Drift detection: persist diff snapshots and compare against the prior run.

A *snapshot* captures the set of permission rows for a pairing at a point in
time. Comparing the latest two snapshots yields the **drift** — rows that
appeared or disappeared — which is what turns the hub from a manual diff tool
into a monitor.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Optional

from app.services import db


def _row_key(row: Any) -> str:
    """Stable identity for a diff row (works for DiffRow objects or dicts)."""
    get = (lambda k: getattr(row, k, "")) if not isinstance(row, dict) else row.get
    return "|".join(
        str(get(k) or "")
        for k in ("principal_key", "securable_scope", "access_class",
                  "constraint_kind", "constraint_key")
    )


def _serialize_row(row: Any) -> dict:
    if isinstance(row, dict):
        return {k: row.get(k) for k in (
            "principal_key", "principal_display", "principal_type",
            "securable_scope", "access_class", "constraint_kind",
            "constraint_key", "on_dbx", "on_fabric")}
    return {
        "principal_key": getattr(row, "principal_key", ""),
        "principal_display": getattr(row, "principal_display", ""),
        "principal_type": getattr(row, "principal_type", ""),
        "securable_scope": getattr(row, "securable_scope", ""),
        "access_class": getattr(row, "access_class", ""),
        "constraint_kind": getattr(row, "constraint_kind", ""),
        "constraint_key": getattr(row, "constraint_key", ""),
        "on_dbx": getattr(row, "on_dbx", False),
        "on_fabric": getattr(row, "on_fabric", False),
    }


def record_snapshot(pairing_id: str, buckets: dict) -> dict:
    """Persist a snapshot of the current diff for a pairing.

    ``buckets`` is the structure returned by ``pair_diff.compute_diff`` with
    keys ``all`` / ``dbx_only`` / ``fabric_only`` / ``in_sync``.
    Returns a summary dict including drift versus the previous snapshot.
    """
    db.init_db()
    all_rows = buckets.get("all") or []
    serialized = [_serialize_row(r) for r in all_rows]
    keys = sorted(_row_key(r) for r in all_rows)
    fingerprint = hashlib.sha256("\n".join(keys).encode("utf-8")).hexdigest()

    prev = db.query_one(
        "SELECT id, fingerprint, rows_json FROM diff_snapshots "
        "WHERE pairing_id = ? ORDER BY taken_at DESC, id DESC LIMIT 1",
        (pairing_id,),
    )

    cur = db.execute(
        """INSERT INTO diff_snapshots
             (pairing_id, taken_at, row_count, dbx_only, fabric_only,
              in_sync, fingerprint, rows_json)
           VALUES (?,?,?,?,?,?,?,?)""",
        (
            pairing_id, db.utcnow(), len(all_rows),
            len(buckets.get("dbx_only") or []),
            len(buckets.get("fabric_only") or []),
            len(buckets.get("in_sync") or []),
            fingerprint, json.dumps(serialized, separators=(",", ":")),
        ),
    )

    drift = _drift_from(prev, serialized)
    drift["snapshot_id"] = cur.lastrowid
    drift["fingerprint"] = fingerprint
    drift["unchanged_since_previous"] = bool(prev and prev["fingerprint"] == fingerprint)
    return drift


def _drift_from(prev: Optional[dict], current_rows: list[dict]) -> dict:
    if not prev:
        return {"added": [], "removed": [], "is_first": True}
    try:
        prev_rows = json.loads(prev["rows_json"])
    except Exception:
        prev_rows = []
    prev_keys = {_row_key(r): r for r in prev_rows}
    cur_keys = {_row_key(r): r for r in current_rows}
    added = [cur_keys[k] for k in cur_keys.keys() - prev_keys.keys()]
    removed = [prev_keys[k] for k in prev_keys.keys() - cur_keys.keys()]
    return {"added": added, "removed": removed, "is_first": False}


def latest_snapshot(pairing_id: str) -> Optional[dict]:
    db.init_db()
    return db.query_one(
        "SELECT id, taken_at, row_count, dbx_only, fabric_only, in_sync, fingerprint "
        "FROM diff_snapshots WHERE pairing_id = ? ORDER BY taken_at DESC, id DESC LIMIT 1",
        (pairing_id,),
    )


def snapshot_history(pairing_id: str, limit: int = 30) -> list[dict]:
    db.init_db()
    return db.query(
        "SELECT id, taken_at, row_count, dbx_only, fabric_only, in_sync, fingerprint "
        "FROM diff_snapshots WHERE pairing_id = ? ORDER BY taken_at DESC, id DESC LIMIT ?",
        (pairing_id, limit),
    )
