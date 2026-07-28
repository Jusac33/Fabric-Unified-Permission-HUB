#!/usr/bin/env python3
"""Read-only probe for Unity Catalog ABAC policies.

Walks the configured catalog (catalog -> schemas -> tables) and calls the
ABAC policies API at each level via DatabricksUCClient.list_policies, then
prints the raw payloads and how pair_diff would surface them.

Usage:
  .venv\\Scripts\\python.exe scripts/probe_abac_policies.py
"""
from __future__ import annotations
import json
import os
import sys

# -- Ensure Azure CLI on PATH --
_AZ_DIR = r"C:\Program Files\Microsoft SDKs\Azure\CLI2\wbin"
if os.path.isdir(_AZ_DIR) and _AZ_DIR not in os.environ.get("PATH", ""):
    os.environ["PATH"] = _AZ_DIR + os.pathsep + os.environ.get("PATH", "")

sys.path.insert(0, ".")

from app.services.databricks_rest import DatabricksUCClient
from app.services.pair_diff import collect_dbx_rows

# ===================== CONFIGURATION (env-driven) =====================
# Set DBX_WORKSPACE_URL and UC_CATALOG in .env or the shell before running.
DBX_URL = os.environ["DBX_WORKSPACE_URL"]
UC_CATALOG = os.environ["UC_CATALOG"]
MAX_SCHEMAS = 25
MAX_TABLES_PER_SCHEMA = 25
# ======================================================================


def _dump(label: str, payload) -> None:
    print(f"\n--- {label} ---")
    print(json.dumps(payload, indent=2, default=str)[:4000])


def main() -> None:
    print("=" * 70)
    print("  UC ABAC POLICY PROBE (read-only)")
    print(f"  Workspace: {DBX_URL}")
    print(f"  Catalog:   {UC_CATALOG}")
    print("=" * 70)

    client = DatabricksUCClient(DBX_URL)

    # Connectivity check
    try:
        cats = client.list_catalogs()
        print(f"\nConnected. {len(cats)} catalog(s) visible.")
    except Exception as exc:  # noqa: BLE001
        print(f"\nFAILED to connect: {exc}")
        sys.exit(1)

    total_policies = 0

    # Catalog-level
    cat_policies = client.list_policies("catalog", UC_CATALOG)
    print(f"\n[catalog] {UC_CATALOG}: {len(cat_policies)} ABAC policy(ies)")
    if cat_policies:
        _dump(f"catalog:{UC_CATALOG}", cat_policies)
    total_policies += len(cat_policies)

    # Schema- and table-level
    try:
        schemas = client.list_schemas(UC_CATALOG)
    except Exception as exc:  # noqa: BLE001
        print(f"  could not list schemas: {exc}")
        schemas = []

    for s in schemas[:MAX_SCHEMAS]:
        sname = s.get("name")
        if not sname or sname == "information_schema":
            continue
        sfqn = f"{UC_CATALOG}.{sname}"
        sch_policies = client.list_policies("schema", sfqn)
        if sch_policies:
            print(f"\n[schema] {sfqn}: {len(sch_policies)} ABAC policy(ies)")
            _dump(f"schema:{sfqn}", sch_policies)
        total_policies += len(sch_policies)

        try:
            tables = client.list_tables(UC_CATALOG, sname)
        except Exception as exc:  # noqa: BLE001
            print(f"  could not list tables in {sfqn}: {exc}")
            tables = []

        for t in tables[:MAX_TABLES_PER_SCHEMA]:
            tname = t.get("name")
            if not tname:
                continue
            tfqn = f"{sfqn}.{tname}"
            tbl_policies = client.list_policies("table", tfqn)
            if tbl_policies:
                print(f"\n[table] {tfqn}: {len(tbl_policies)} ABAC policy(ies)")
                _dump(f"table:{tfqn}", tbl_policies)
            total_policies += len(tbl_policies)

    print("\n" + "=" * 70)
    print(f"  Raw ABAC policies discovered: {total_policies}")
    print("=" * 70)

    # Now show how the diff engine surfaces them end-to-end
    print("\nRunning collect_dbx_rows() to confirm ABAC rows appear in the diff...")
    rows = collect_dbx_rows(DBX_URL, UC_CATALOG)
    abac_rows = [r for r in rows if r.constraint_details.get("source") == "databricks_abac"]
    print(f"  ABAC diff rows: {len(abac_rows)}")
    for r in abac_rows:
        d = r.constraint_details
        print(f"   - {r.constraint_kind:12s} scope={r.securable_scope} "
              f"policy={d.get('policy_name')!r} fn={d.get('function')!r} "
              f"when={d.get('when_condition')!r}")

    if total_policies == 0:
        print("\nNOTE: No ABAC policies found. Either none are defined on this "
              "catalog, or ABAC is not enabled on this workspace. The empty "
              "result confirms graceful degradation either way.")


if __name__ == "__main__":
    main()
