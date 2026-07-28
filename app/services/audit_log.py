"""Append-only audit log for permission-changing workflows."""
from __future__ import annotations

import json
import uuid
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.config import settings

AUDIT_FILE = "permission-applies.jsonl"


def _json_default(value: Any) -> Any:
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"{type(value).__name__} is not JSON serializable")


def _report_counts(report: Any | None) -> dict[str, int]:
    if report is None:
        return {"ok": 0, "skipped": 0, "failed": 0}
    return {
        "ok": int(report.n_ok),
        "skipped": int(report.n_skipped),
        "failed": int(report.n_failed),
    }


def _status(error: str | None, counts: dict[str, int]) -> str:
    if error:
        return "blocked"
    if counts["failed"] == 0:
        return "completed"
    if counts["ok"] > 0 or counts["skipped"] > 0:
        return "partial"
    return "failed"


def _write_record(record: dict[str, Any]) -> None:
    path = settings.audit_path / AUDIT_FILE
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, default=_json_default, sort_keys=True) + "\n")


def record_permission_apply(
    *,
    pairing: dict,
    direction: str,
    dry_run: bool,
    selected_count: int,
    report: Any | None,
    error: str | None,
    actor: dict[str, str],
    audit_id: str | None = None,
    event: str = "permission_apply",
    status_override: str | None = None,
) -> str:
    """Persist a sanitized audit record and return its audit id."""
    audit_id = audit_id or uuid.uuid4().hex
    counts = _report_counts(report)
    record = {
        "id": audit_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event": event,
        "dry_run": dry_run,
        "direction": direction,
        "selected_count": selected_count,
        "status": status_override or _status(error, counts),
        "error": error,
        "actor": actor,
        "pairing": {
            "id": pairing.get("id"),
            "label": pairing.get("label"),
            "fabric_workspace_id": pairing.get("fabric_workspace_id"),
            "uc_catalog": pairing.get("uc_catalog"),
            "dbx_workspace_url": pairing.get("dbx_workspace_url"),
        },
        "counts": counts,
        "actions": getattr(report, "actions", []) if report else [],
    }

    _write_record(record)
    _write_db_event(record)
    return audit_id


def _write_db_event(record: dict[str, Any]) -> None:
    """Mirror the audit record into the queryable DB table (best-effort)."""
    try:
        from app.services import db

        counts = record.get("counts") or {}
        pairing = record.get("pairing") or {}
        detail = {
            "actions": [
                {
                    "principal": getattr(a, "principal", None) if not isinstance(a, dict) else a.get("principal"),
                    "scope": getattr(a, "securable_scope", None) if not isinstance(a, dict) else a.get("securable_scope"),
                    "layer": getattr(a, "layer", None) if not isinstance(a, dict) else a.get("layer"),
                    "ok": getattr(a, "ok", None) if not isinstance(a, dict) else a.get("ok"),
                    "skipped": getattr(a, "skipped", None) if not isinstance(a, dict) else a.get("skipped"),
                    "message": getattr(a, "message", None) if not isinstance(a, dict) else a.get("message"),
                }
                for a in (record.get("actions") or [])
            ]
        }
        db.init_db()
        db.execute(
            """INSERT OR REPLACE INTO audit_events
                 (id, timestamp, event, pairing_id, direction, dry_run, status,
                  selected_count, ok_count, skipped_count, failed_count,
                  actor, error, detail_json)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                record["id"], record["timestamp"], record["event"],
                pairing.get("id"), record.get("direction"),
                1 if record.get("dry_run") else 0, record.get("status"),
                int(record.get("selected_count") or 0),
                int(counts.get("ok") or 0), int(counts.get("skipped") or 0),
                int(counts.get("failed") or 0),
                json.dumps(record.get("actor") or {}),
                record.get("error"),
                json.dumps(detail, default=_json_default),
            ),
        )
    except Exception:
        # Audit DB mirror is best-effort; the JSONL file remains the source of record.
        pass


def list_audit_events(limit: int = 100, pairing_id: str | None = None) -> list[dict]:
    """Read recent audit events from the queryable DB table."""
    from app.services import db

    db.init_db()
    if pairing_id:
        return db.query(
            "SELECT * FROM audit_events WHERE pairing_id = ? "
            "ORDER BY timestamp DESC LIMIT ?",
            (pairing_id, limit),
        )
    return db.query(
        "SELECT * FROM audit_events ORDER BY timestamp DESC LIMIT ?", (limit,)
    )


def record_permission_apply_start(
    *,
    pairing: dict,
    direction: str,
    dry_run: bool,
    selected_count: int,
    actor: dict[str, str],
) -> str:
    """Write a durable start record before any real permission changes."""
    audit_id = uuid.uuid4().hex
    record_permission_apply(
        pairing=pairing,
        direction=direction,
        dry_run=dry_run,
        selected_count=selected_count,
        report=None,
        error=None,
        actor=actor,
        audit_id=audit_id,
        event="permission_apply_started",
        status_override="started",
    )
    return audit_id
