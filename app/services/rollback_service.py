"""Rollback plan generation for a recorded apply.

Reads a stored audit event's successfully-applied actions and produces the
*reverse plan* — the set of revoke operations that would undo them. This is
read-only and safe: it does not execute revokes. Automated revoke execution is
intentionally not performed here because it writes to production permission
surfaces; an operator reviews the plan and acts deliberately.
"""
from __future__ import annotations

import json
from typing import Optional

from app.services import db


def _reverse_action(action: dict) -> Optional[dict]:
    """Map one applied action to its reverse operation descriptor."""
    if not action.get("ok") or action.get("skipped"):
        return None  # nothing was actually changed
    layer = (action.get("layer") or "").lower()
    principal = action.get("principal") or ""
    scope = action.get("scope") or ""

    if layer == "workspace_role":
        return {
            "operation": "remove_workspace_role",
            "principal": principal,
            "scope": scope,
            "description": f"Remove workspace role granted to {principal}",
        }
    if layer == "onelake_dar":
        return {
            "operation": "remove_onelake_dar_member",
            "principal": principal,
            "scope": scope,
            "description": f"Remove {principal} from the OneLake DAR role on {scope}",
        }
    if layer in ("row_filter", "column_mask"):
        return {
            "operation": "remove_fine_grained_constraint",
            "principal": principal,
            "scope": scope,
            "description": f"Remove the {layer} constraint applied on {scope}",
        }
    return {
        "operation": "manual_review",
        "principal": principal,
        "scope": scope,
        "description": f"Manual review required to reverse '{layer}' on {scope}",
    }


def build_plan(audit_id: str) -> Optional[dict]:
    """Return a reverse plan for a recorded apply event, or None if not found."""
    db.init_db()
    event = db.query_one("SELECT * FROM audit_events WHERE id = ?", (audit_id,))
    if not event:
        return None
    if event.get("dry_run"):
        return {
            "audit_id": audit_id,
            "executable": False,
            "reason": "This was a dry-run; nothing was applied, so there is nothing to roll back.",
            "steps": [],
        }
    try:
        detail = json.loads(event.get("detail_json") or "{}")
    except Exception:
        detail = {}
    steps = []
    for action in detail.get("actions") or []:
        rev = _reverse_action(action)
        if rev:
            steps.append(rev)
    return {
        "audit_id": audit_id,
        "pairing_id": event.get("pairing_id"),
        "direction": event.get("direction"),
        "applied_at": event.get("timestamp"),
        "executable": False,  # plan is review-only; revoke is performed manually
        "reason": "Review-only plan. Revoke operations are not auto-executed to "
                  "protect production permission surfaces.",
        "steps": steps,
    }
