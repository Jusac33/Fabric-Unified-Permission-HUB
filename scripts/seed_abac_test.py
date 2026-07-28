#!/usr/bin/env python3
"""Seed a throwaway ABAC setup on the test catalog to prove the resolver pipeline.

Creates (idempotent where possible) in the catalog named by `UC_CATALOG`,
schema `uph_test`:
  * a masking UDF                       uph_test.mask_ssn_abac
  * a governed tag + a column tag       pii:ssn on orders.ssn
  * a COLUMN MASK ABAC policy           via CREATE POLICY (SQL) or REST fallback

All statements run via the SQL warehouse. This is a WRITE operation, intended for
a disposable test catalog only.

Usage:
  .venv\\Scripts\\python.exe scripts/seed_abac_test.py
"""
from __future__ import annotations
import os
import sys

_AZ_DIR = r"C:\Program Files\Microsoft SDKs\Azure\CLI2\wbin"
if os.path.isdir(_AZ_DIR) and _AZ_DIR not in os.environ.get("PATH", ""):
    os.environ["PATH"] = _AZ_DIR + os.pathsep + os.environ.get("PATH", "")

sys.path.insert(0, ".")

from app.services.databricks_rest import DatabricksUCClient

# Env-driven — set DBX_WORKSPACE_URL, UC_CATALOG and DBX_WAREHOUSE_ID first.
URL = os.environ["DBX_WORKSPACE_URL"]
CATALOG = os.environ["UC_CATALOG"]
SCHEMA = "uph_test"
TABLE = "orders"
WAREHOUSE = os.environ["DBX_WAREHOUSE_ID"]
TAG_KEY = "pii"
TAG_VALUE = "ssn"
COLUMN = "ssn"


def run(client: DatabricksUCClient, sql: str, *, ignore_errors: bool = False) -> None:
    label = sql.strip().splitlines()[0][:80]
    try:
        client.execute_sql(WAREHOUSE, sql)
        print(f"  [ok]   {label}")
    except Exception as exc:  # noqa: BLE001
        msg = str(exc)[:300]
        if ignore_errors:
            print(f"  [skip] {label}\n         -> {msg}")
        else:
            print(f"  [FAIL] {label}\n         -> {msg}")
            raise


def main() -> None:
    print("=" * 70)
    print("  SEED ABAC TEST (write) on", f"{CATALOG}.{SCHEMA}")
    print("=" * 70)
    c = DatabricksUCClient(URL)
    c.ensure_sql_warehouse_running(WAREHOUSE)

    fq_table = f"{CATALOG}.{SCHEMA}.{TABLE}"

    print("\n[1] ensure schema + table exist")
    run(c, f"CREATE SCHEMA IF NOT EXISTS {CATALOG}.{SCHEMA}", ignore_errors=True)
    run(c, f"""CREATE TABLE IF NOT EXISTS {fq_table} (
                 id BIGINT, region STRING, amount DOUBLE, ssn STRING)
               USING DELTA""", ignore_errors=True)

    print("\n[2] create governed tag (account-level allowed values)")
    # Governed tag DDL — syntax may vary by workspace; ignore if already exists or unsupported.
    run(c, f"CREATE TAG IF NOT EXISTS {TAG_KEY} ALLOWED VALUES ('{TAG_VALUE}', 'email', 'phone')",
        ignore_errors=True)

    print("\n[3] apply tag to the column")
    run(c, f"ALTER TABLE {fq_table} ALTER COLUMN {COLUMN} SET TAGS ('{TAG_KEY}' = '{TAG_VALUE}')",
        ignore_errors=True)

    print("\n[4] create masking UDF")
    run(c, f"DROP FUNCTION IF EXISTS {CATALOG}.{SCHEMA}.mask_ssn_abac", ignore_errors=True)
    run(c, f"""CREATE FUNCTION {CATALOG}.{SCHEMA}.mask_ssn_abac(val STRING)
               RETURNS STRING
               RETURN CONCAT('***-**-', RIGHT(val, 4))""")

    print("\n[5] create COLUMN MASK ABAC policy")
    run(c, "DROP POLICY IF EXISTS mask_ssn_policy ON CATALOG " + CATALOG, ignore_errors=True)
    run(c, f"""CREATE POLICY mask_ssn_policy
               ON CATALOG {CATALOG}
               COLUMN MASK {CATALOG}.{SCHEMA}.mask_ssn_abac
               TO `account users`
               FOR TABLES
               MATCH COLUMNS has_tag_value('{TAG_KEY}', '{TAG_VALUE}') AS ssn_col
               ON COLUMN ssn_col""", ignore_errors=True)

    print("\n[6] verify policy is visible via REST")
    policies = c.list_policies("catalog", CATALOG)
    print(f"  catalog policies now: {len(policies)}")
    for p in policies:
        print("   -", p.get("name"), p.get("policy_type"),
              "->", (p.get("column_mask") or {}).get("function_name"))

    print("\nDone. Now run: scripts/probe_abac_policies.py")


if __name__ == "__main__":
    main()
