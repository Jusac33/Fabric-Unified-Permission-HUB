"""Capture a complete, restorable snapshot of BOTH sides of a pairing.

Read-only. Writes a single JSON file into ``audits/`` following the existing
``fabric-dar-reset-*.json`` convention. This is the restore point that must
exist before any permission cleanup is attempted.

Usage:
    python scripts/backup_permissions.py <pairing_id>
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx

from app.config import settings
from app.services import pairings as pairings_service
from app.services.azure_identity import get_fabric_token
from app.services.databricks_rest import DatabricksUCClient
from app.services.fabric_rest import list_items

FABRIC_API = "https://api.fabric.microsoft.com/v1"


def _fabric_get(path: str) -> tuple[int, dict]:
    r = httpx.get(
        f"{FABRIC_API}{path}",
        headers={"Authorization": f"Bearer {get_fabric_token()}"},
        timeout=30,
    )
    try:
        return r.status_code, (r.json() or {})
    except ValueError:
        return r.status_code, {}


def capture_fabric(workspace_id: str, uc_catalog: str) -> dict:
    status, body = _fabric_get(f"/workspaces/{workspace_id}/roleAssignments")
    role_assignments = body.get("value", []) if status < 300 else []

    items = [
        it for it in list_items(workspace_id)
        if (it.get("displayName") or "").lower() == uc_catalog.lower()
    ]
    per_item = []
    for item in items:
        item_id = item.get("id")
        st, dar = _fabric_get(
            f"/workspaces/{workspace_id}/items/{item_id}/dataAccessRoles"
        )
        per_item.append({
            "item": item,
            "data_access_roles_http_status": st,
            "data_access_roles": dar.get("value", []) if st < 300 else [],
        })

    return {
        "workspace_id": workspace_id,
        "role_assignments_http_status": status,
        "workspace_role_assignments": role_assignments,
        "mirrored_items": per_item,
    }


def capture_databricks(workspace_url: str, catalog: str) -> dict:
    client = DatabricksUCClient(workspace_url)
    securables: list[dict] = []

    def _grab(securable_type: str, full_name: str) -> None:
        try:
            grants = client.get_grants(securable_type, full_name)
        except Exception as exc:  # keep going; record the gap explicitly
            securables.append({
                "securable_type": securable_type,
                "full_name": full_name,
                "error": str(exc),
            })
            return
        if grants:
            securables.append({
                "securable_type": securable_type,
                "full_name": full_name,
                "privilege_assignments": grants,
            })

    _grab("catalog", catalog)
    for schema in client.list_schemas(catalog):
        name = schema.get("name")
        if not name or name == "information_schema":
            continue
        _grab("schema", f"{catalog}.{name}")
        for table in client.list_tables(catalog, name):
            _grab("table", f"{catalog}.{name}.{table.get('name')}")

    entries = sum(
        len(a.get("privileges") or [])
        for s in securables
        for a in (s.get("privilege_assignments") or [])
    )
    return {
        "workspace_url": workspace_url,
        "catalog": catalog,
        "securables": securables,
        "privilege_entry_count": entries,
    }


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    pairing_id = sys.argv[1]
    pairing = pairings_service.get_pairing(pairing_id)
    if not pairing:
        print(f"pairing '{pairing_id}' not found")
        return 1

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    snapshot = {
        "timestamp": stamp,
        "kind": "full-pairing-permission-backup",
        "pairing": pairing,
        "fabric": capture_fabric(
            pairing["fabric_workspace_id"], pairing["uc_catalog"]
        ),
        "databricks": capture_databricks(
            pairing["dbx_workspace_url"], pairing["uc_catalog"]
        ),
    }

    out = settings.audit_path / f"pairing-backup-{pairing_id}-{stamp}.json"
    out.write_text(json.dumps(snapshot, indent=2), encoding="utf-8")

    fab = snapshot["fabric"]
    dar_total = sum(
        len(i["data_access_roles"]) for i in fab["mirrored_items"]
    )
    print(f"wrote {out}")
    print(f"  fabric workspace role assignments : {len(fab['workspace_role_assignments'])}")
    print(f"  fabric OneLake data access roles  : {dar_total}")
    print(f"  databricks securables with grants : {len(snapshot['databricks']['securables'])}")
    print(f"  databricks privilege entries      : {snapshot['databricks']['privilege_entry_count']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
