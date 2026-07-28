"""Seed ~10 test permissions on Databricks (UC) AND Fabric for end-to-end testing.

Side effects (idempotent where possible):

DBX side (catalog named by `UC_CATALOG`):
  * CREATE SCHEMA  uph_test
  * CREATE TABLE   uph_test.orders          (id, region, amount, ssn, customer_email)
  * CREATE TABLE   uph_test.customers       (id, name, email, country)
  * CREATE FUNCTION uph_test.mask_ssn(...)            -- column mask
  * CREATE FUNCTION uph_test.row_filter_region(...)   -- row filter
  * ALTER TABLE uph_test.orders ALTER COLUMN ssn SET MASK uph_test.mask_ssn
  * ALTER TABLE uph_test.orders SET ROW FILTER uph_test.row_filter_region ON (region)
  * ~6 UC GRANTs (catalog/schema/table levels, SELECT / USE_SCHEMA / MODIFY)

Fabric side (workspace named by `FABRIC_WORKSPACE_ID`):
  * 4 workspace role assignments (Viewer/Contributor) — Entra users in tenant
  * (OneLake DAR is best added through the hub UI on the mirrored item)

Required environment variables:
    DBX_WORKSPACE_URL, DBX_WAREHOUSE_ID, UC_CATALOG,
    FABRIC_WORKSPACE_ID, UC_TEST_PRINCIPAL

Run:
    & .\.venv\Scripts\python.exe scripts\seed_test_perms.py
"""
from __future__ import annotations
import os, sys, time, uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx
from app.services.azure_identity import get_token, get_fabric_token
from app.services.databricks_rest import DBX_RESOURCE_ID
from app.services import fabric_rest

DBX_URL = os.environ["DBX_WORKSPACE_URL"]
WAREHOUSE_ID = os.environ["DBX_WAREHOUSE_ID"]
CATALOG = os.environ["UC_CATALOG"]
SCHEMA = "uph_test"
FABRIC_WS = os.environ["FABRIC_WORKSPACE_ID"]

# Test principals — must exist in your tenant for Fabric grants to succeed
TEST_PRINCIPAL = os.environ["UC_TEST_PRINCIPAL"]
TEST_USERS = [TEST_PRINCIPAL]
# Groups to GRANT in UC (these stay DBX-side)
TEST_DBX_GROUPS = ["account users"]


# ---------------- DBX SQL execution ----------------
def _dbx_token() -> str:
    return get_token(f"{DBX_RESOURCE_ID}/.default")


def _retry(fn, what: str, attempts: int = 5):
    last = None
    for i in range(attempts):
        try:
            return fn()
        except (httpx.ConnectError, httpx.ReadError, httpx.RemoteProtocolError) as e:
            last = e
            print(f"  [retry {i+1}/{attempts}] {what}: {type(e).__name__}")
            time.sleep(2 + i)
    raise last


def _ensure_warehouse_running():
    h = {"Authorization": f"Bearer {_dbx_token()}"}
    r = _retry(lambda: httpx.get(f"{DBX_URL}/api/2.0/sql/warehouses/{WAREHOUSE_ID}",
                                 headers=h, timeout=30), "get warehouse").json()
    state = r.get("state")
    if state == "RUNNING":
        return
    print(f"[wh] state={state}, starting...")
    _retry(lambda: httpx.post(f"{DBX_URL}/api/2.0/sql/warehouses/{WAREHOUSE_ID}/start",
                              headers=h, timeout=30), "start warehouse")
    for i in range(60):
        time.sleep(5)
        r = _retry(lambda: httpx.get(f"{DBX_URL}/api/2.0/sql/warehouses/{WAREHOUSE_ID}",
                                     headers=h, timeout=30), "poll warehouse").json()
        if r.get("state") == "RUNNING":
            print(f"[wh] running after {(i+1)*5}s")
            return
    raise RuntimeError("warehouse did not start in 5 minutes")


def sql(stmt: str, *, ignore_errors: bool = False) -> dict:
    """Run a single SQL statement on the warehouse, wait for completion."""
    h = {"Authorization": f"Bearer {_dbx_token()}",
         "Content-Type": "application/json"}
    body = {"warehouse_id": WAREHOUSE_ID, "statement": stmt,
            "wait_timeout": "30s", "on_wait_timeout": "CONTINUE"}
    r = _retry(lambda: httpx.post(f"{DBX_URL}/api/2.0/sql/statements", json=body,
                                  headers=h, timeout=60), "submit sql").json()
    sid = r.get("statement_id")
    status = (r.get("status") or {}).get("state")
    while status in ("PENDING", "RUNNING"):
        time.sleep(2)
        r = _retry(lambda: httpx.get(f"{DBX_URL}/api/2.0/sql/statements/{sid}",
                                     headers=h, timeout=30), "poll sql").json()
        status = (r.get("status") or {}).get("state")
    if status != "SUCCEEDED":
        msg = (r.get("status") or {}).get("error", {}).get("message", str(r))
        if ignore_errors:
            print(f"  [skip] {stmt[:60]}... -> {msg[:120]}")
            return r
        raise RuntimeError(f"SQL FAILED: {stmt}\n  -> {msg}")
    return r


def seed_dbx_data_and_perms():
    print("\n=== Seeding Databricks ===")
    _ensure_warehouse_running()

    print("[ddl] schema + tables")
    sql(f"CREATE SCHEMA IF NOT EXISTS {CATALOG}.{SCHEMA} COMMENT 'UPH e2e test'")
    sql(f"DROP TABLE IF EXISTS {CATALOG}.{SCHEMA}.orders")
    sql(f"""CREATE TABLE {CATALOG}.{SCHEMA}.orders (
              id BIGINT, region STRING, amount DECIMAL(10,2),
              ssn STRING, customer_email STRING
            ) USING DELTA""")
    sql(f"""INSERT INTO {CATALOG}.{SCHEMA}.orders VALUES
            (1,'EMEA',100.00,'111-22-3333','a@x.com'),
            (2,'AMER',200.50,'222-33-4444','b@x.com'),
            (3,'APAC', 50.75,'333-44-5555','c@x.com'),
            (4,'EMEA', 75.25,'444-55-6666','d@x.com')""")

    sql(f"DROP TABLE IF EXISTS {CATALOG}.{SCHEMA}.customers")
    sql(f"""CREATE TABLE {CATALOG}.{SCHEMA}.customers (
              id BIGINT, name STRING, email STRING, country STRING
            ) USING DELTA""")
    sql(f"""INSERT INTO {CATALOG}.{SCHEMA}.customers VALUES
            (1,'Alice','a@x.com','DE'),(2,'Bob','b@x.com','US')""")

    print("[ddl] column mask + row filter functions")
    sql(f"DROP FUNCTION IF EXISTS {CATALOG}.{SCHEMA}.mask_ssn")
    sql(f"""CREATE FUNCTION {CATALOG}.{SCHEMA}.mask_ssn(ssn STRING)
            RETURNS STRING
            RETURN CASE
              WHEN is_account_group_member('analysts') THEN ssn
              ELSE 'XXX-XX-' || substring(ssn, -4, 4)
            END""")

    sql(f"DROP FUNCTION IF EXISTS {CATALOG}.{SCHEMA}.row_filter_region")
    sql(f"""CREATE FUNCTION {CATALOG}.{SCHEMA}.row_filter_region(region STRING)
            RETURNS BOOLEAN
            RETURN
              is_account_group_member('account users')
              AND region IN ('EMEA','AMER','APAC')""")

    print("[ddl] apply column mask + row filter on orders")
    # Column mask on `ssn`
    sql(f"""ALTER TABLE {CATALOG}.{SCHEMA}.orders
            ALTER COLUMN ssn SET MASK {CATALOG}.{SCHEMA}.mask_ssn""",
        ignore_errors=True)
    # Row filter on region
    sql(f"""ALTER TABLE {CATALOG}.{SCHEMA}.orders
            SET ROW FILTER {CATALOG}.{SCHEMA}.row_filter_region ON (region)""",
        ignore_errors=True)

    print("[grant] UC permissions (mix of catalog/schema/table)")
    grants = [
        # 1
        f"GRANT USE_SCHEMA ON SCHEMA {CATALOG}.{SCHEMA} TO `account users`",
        # 2
        f"GRANT SELECT ON TABLE {CATALOG}.{SCHEMA}.orders TO `account users`",
        # 3
        f"GRANT SELECT ON TABLE {CATALOG}.{SCHEMA}.customers TO `account users`",
        # 4
        f"GRANT MODIFY ON TABLE {CATALOG}.{SCHEMA}.orders TO `{TEST_PRINCIPAL}`",
        # 5
        f"GRANT SELECT ON SCHEMA {CATALOG}.{SCHEMA} TO `{TEST_PRINCIPAL}`",
        # 6
        f"GRANT EXECUTE ON FUNCTION {CATALOG}.{SCHEMA}.mask_ssn TO `account users`",
    ]
    for g in grants:
        sql(g, ignore_errors=True)

    print("[done] DBX seeded.\n")


# ---------------- Fabric grants ----------------
def _fabric_post(path: str, body: dict) -> tuple[int, str]:
    tok = get_fabric_token()
    r = httpx.post(f"https://api.fabric.microsoft.com{path}",
                   json=body,
                   headers={"Authorization": f"Bearer {tok}",
                            "Content-Type": "application/json"},
                   timeout=60)
    return r.status_code, r.text[:300]


def _resolve_user_oid(upn: str) -> str | None:
    tok = get_token("https://graph.microsoft.com/.default")
    r = httpx.get("https://graph.microsoft.com/v1.0/users",
                  headers={"Authorization": f"Bearer {tok}"},
                  params={"$filter": f"userPrincipalName eq '{upn}' or mail eq '{upn}'",
                          "$select": "id,displayName"},
                  timeout=15)
    if r.status_code != 200:
        return None
    vals = r.json().get("value") or []
    return vals[0]["id"] if vals else None


def seed_fabric_perms():
    print("\n=== Seeding Fabric ===")
    pairs = [
        # role, upn — adjust to taste
        ("Viewer", TEST_PRINCIPAL),
    ]
    # add a couple of synthetic test users if you want; skipped here to avoid 403s
    for role, upn in pairs:
        oid = _resolve_user_oid(upn)
        if not oid:
            print(f"[skip] {upn}: not in tenant")
            continue
        body = {"principal": {"id": oid, "type": "User"}, "role": role}
        code, txt = _fabric_post(
            f"/v1/workspaces/{FABRIC_WS}/roleAssignments", body)
        if code in (200, 201):
            print(f"[ok]  GRANT {role} -> {upn}")
        elif code == 409:
            print(f"[ok]  {upn} already has a role (409 idempotent)")
        else:
            print(f"[err] {upn}: {code} {txt}")
    print("[done] Fabric seeded.\n")


def main():
    seed_dbx_data_and_perms()
    seed_fabric_perms()
    print("=========================================")
    print("Now visit: http://127.0.0.1:8000/pairings/deb6ae26a3/diff")
    print("Refresh DBX/Fabric inventories first if cached.")
    print("=========================================")


if __name__ == "__main__":
    main()
