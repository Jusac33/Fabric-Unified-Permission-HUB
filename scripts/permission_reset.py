"""Cleanup / restore permissions for a pairing, for demo and re-sync scenarios.

Dry-run by default — nothing is written unless ``--apply`` is passed.

    python scripts/permission_reset.py cleanup <pairing_id> [--apply] [--include-system]
                                               [--side=both|fabric|dbx]
    python scripts/permission_reset.py restore <backup_file.json> [--apply]

HARD GUARD: Fabric *workspace role assignments* are never touched. The paired
workspace has a single Admin, which is also the identity this tool authenticates
as; removing it would be an unrecoverable lockout.

Unity Catalog owners retain control independently of grants, so revoking grants
is recoverable. ``restore`` replays a backup produced by
``scripts/backup_permissions.py`` and does not depend on the sync engine.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx

from app.services import pairings as pairings_service
from app.services.azure_identity import get_fabric_token
from app.services.databricks_rest import DatabricksUCClient
from app.services.fabric_rest import list_items

FABRIC_API = "https://api.fabric.microsoft.com/v1"

# Databricks *system-generated* groups. The UC API rejects any attempt to grant
# privileges to these ("Cannot grant privileges on catalog to system generated
# group ..."), so revoking them is IRREVERSIBLE. Never revoke, never restore.
SYSTEM_GENERATED_PREFIXES = ("_workspace_users_", "_workspace_admins_")

# Built-in account group. Revocable and restorable, but the sync engine cannot
# recreate it (no Entra identity), so it is preserved unless --include-system.
BUILTIN_PRINCIPALS = {"account users"}


def _is_system_generated(principal: str) -> bool:
    """Principals the UC API refuses to grant to — revoking is unrecoverable."""
    return (principal or "").lower().startswith(SYSTEM_GENERATED_PREFIXES)


def _is_builtin_principal(principal: str) -> bool:
    return (principal or "").lower() in BUILTIN_PRINCIPALS


def _fabric_headers() -> dict:
    return {
        "Authorization": f"Bearer {get_fabric_token()}",
        "Content-Type": "application/json",
    }


def _mirrored_items(workspace_id: str, uc_catalog: str) -> list[dict]:
    return [
        it for it in list_items(workspace_id)
        if (it.get("displayName") or "").lower() == uc_catalog.lower()
    ]


def _dar_url(workspace_id: str, item_id: str) -> str:
    return f"{FABRIC_API}/workspaces/{workspace_id}/items/{item_id}/dataAccessRoles"


def _get_dar(workspace_id: str, item_id: str) -> list[dict]:
    r = httpx.get(_dar_url(workspace_id, item_id),
                  headers=_fabric_headers(), timeout=30)
    if r.status_code >= 300:
        raise RuntimeError(f"GET dataAccessRoles {r.status_code}: {r.text[:200]}")
    return (r.json() or {}).get("value", []) or []


def _put_dar(workspace_id: str, item_id: str, roles: list[dict]) -> None:
    r = httpx.put(_dar_url(workspace_id, item_id), headers=_fabric_headers(),
                  json={"value": roles}, timeout=60)
    if r.status_code >= 300:
        raise RuntimeError(f"PUT dataAccessRoles {r.status_code}: {r.text[:300]}")


def _uc_securables(client: DatabricksUCClient, catalog: str) -> list[tuple[str, str]]:
    out = [("catalog", catalog)]
    for schema in client.list_schemas(catalog):
        name = schema.get("name")
        if not name or name == "information_schema":
            continue
        out.append(("schema", f"{catalog}.{name}"))
        for table in client.list_tables(catalog, name):
            out.append(("table", f"{catalog}.{name}.{table.get('name')}"))
    return out


def cleanup(pairing_id: str, apply: bool, include_system: bool,
            side: str = "both") -> int:
    pairing = pairings_service.get_pairing(pairing_id)
    if not pairing:
        print(f"pairing '{pairing_id}' not found")
        return 1

    workspace_id = pairing["fabric_workspace_id"]
    catalog = pairing["uc_catalog"]
    mode = "APPLY" if apply else "DRY-RUN"
    print(f"=== cleanup [{mode}] side={side} pairing {pairing_id} "
          f"({pairing.get('label')}) ===\n")

    fabric_removals = 0
    if side in ("both", "fabric"):
        print("-- Fabric: OneLake data access roles --")
        for item in _mirrored_items(workspace_id, catalog):
            item_id = item.get("id")
            try:
                roles = _get_dar(workspace_id, item_id)
            except RuntimeError as exc:
                print(f"  item {item_id[:8]}: {exc}")
                continue
            for role in roles:
                print(f"  remove DAR role  {role.get('name')}")
            fabric_removals += len(roles)
            if apply and roles:
                _put_dar(workspace_id, item_id, [])
                print(f"  -> cleared {len(roles)} roles on item {item_id[:8]}")
        print("  workspace role assignments: PRESERVED (lockout guard)\n")

    uc_removals = 0
    preserved = 0
    if side in ("both", "dbx"):
        print("-- Databricks: Unity Catalog grants --")
        client = DatabricksUCClient(pairing["dbx_workspace_url"])
        for securable_type, full_name in _uc_securables(client, catalog):
            try:
                grants = client.get_grants(securable_type, full_name)
            except Exception as exc:
                print(f"  {full_name}: read error {exc}")
                continue
            changes = []
            for assignment in grants:
                principal = assignment.get("principal")
                privileges = list(assignment.get("privileges") or [])
                if not privileges:
                    continue
                # Hard guard: the UC API cannot grant these back.
                if _is_system_generated(principal):
                    preserved += len(privileges)
                    continue
                if _is_builtin_principal(principal) and not include_system:
                    preserved += len(privileges)
                    continue
                changes.append({"principal": principal, "remove": privileges})
                uc_removals += len(privileges)
                print(f"  revoke {securable_type:7s} {full_name} :: "
                      f"{principal} {sorted(privileges)}")
            if apply and changes:
                client.update_grants(securable_type, full_name, changes)

    print()
    print(f"  DAR roles removed        : {fabric_removals}")
    print(f"  UC privileges revoked    : {uc_removals}")
    print(f"  UC privileges preserved  : {preserved} (system principals)")
    if not apply:
        print("\nDRY-RUN — nothing written. Re-run with --apply to execute.")
    return 0


def restore(backup_file: str, apply: bool) -> int:
    path = Path(backup_file)
    if not path.exists():
        print(f"backup file not found: {path}")
        return 1
    snapshot = json.loads(path.read_text(encoding="utf-8"))
    mode = "APPLY" if apply else "DRY-RUN"
    print(f"=== restore [{mode}] from {path.name} ===\n")

    fabric = snapshot.get("fabric") or {}
    workspace_id = fabric.get("workspace_id")
    print("-- Fabric: OneLake data access roles --")
    restored_roles = 0
    for entry in fabric.get("mirrored_items") or []:
        item_id = (entry.get("item") or {}).get("id")
        roles = entry.get("data_access_roles") or []
        # Strip server-managed fields so the PUT is accepted cleanly.
        payload = [
            {k: v for k, v in role.items() if k not in ("id", "etag")}
            for role in roles
        ]
        for role in payload:
            print(f"  restore DAR role {role.get('name')}")
        restored_roles += len(payload)
        if apply and payload:
            _put_dar(workspace_id, item_id, payload)
            print(f"  -> restored {len(payload)} roles on item {item_id[:8]}")

    print("\n-- Databricks: Unity Catalog grants --")
    databricks = snapshot.get("databricks") or {}
    client = DatabricksUCClient(databricks.get("workspace_url"))
    restored_privileges = 0
    for securable in databricks.get("securables") or []:
        full_name = securable.get("full_name")
        securable_type = securable.get("securable_type")
        changes = []
        for assignment in securable.get("privilege_assignments") or []:
            principal = assignment.get("principal")
            privileges = list(assignment.get("privileges") or [])
            if not privileges:
                continue
            if _is_system_generated(principal):
                print(f"  skip  {securable_type:7s} {full_name} :: "
                      f"{principal} (system-generated, never revoked)")
                continue
            changes.append({"principal": principal, "add": privileges})
            restored_privileges += len(privileges)
            print(f"  grant {securable_type:7s} {full_name} :: "
                  f"{principal} {sorted(privileges)}")
        if apply and changes:
            try:
                client.update_grants(securable_type, full_name, changes)
            except Exception as exc:
                # Never let one securable abort the whole restore.
                print(f"  !! {full_name}: {exc}")

    print()
    print(f"  DAR roles restored     : {restored_roles}")
    print(f"  UC privileges restored : {restored_privileges}")
    if not apply:
        print("\nDRY-RUN — nothing written. Re-run with --apply to execute.")
    return 0


def main() -> int:
    args = sys.argv[1:]
    if not args or args[0] not in ("cleanup", "restore"):
        print(__doc__)
        return 2
    command, rest = args[0], args[1:]
    apply = "--apply" in rest
    positional = [a for a in rest if not a.startswith("--")]
    if not positional:
        print(__doc__)
        return 2
    if command == "cleanup":
        side = "both"
        for token in rest:
            if token.startswith("--side="):
                side = token.split("=", 1)[1]
        if side not in ("both", "fabric", "dbx"):
            print("--side must be one of: both, fabric, dbx")
            return 2
        return cleanup(positional[0], apply, "--include-system" in rest, side)
    return restore(positional[0], apply)


if __name__ == "__main__":
    raise SystemExit(main())
