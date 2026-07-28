#!/usr/bin/env python3
"""Systematic UC privilege → Fabric mirror mapping test.

Phase 1: Grant-validation — test every UC privilege on catalog/schema/table
          to confirm which are grantable via the REST API.
Phase 2: Mapping analysis — for each grantable privilege, determine the
          Fabric mirror equivalent (if any), considering mirrors are read-only.

Usage:
  .venv\Scripts\python.exe scripts/test_uc_permissions.py
"""
from __future__ import annotations
import json
import os
import sys
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
import httpx

# -- Ensure Azure CLI on PATH --
_AZ_DIR = r"C:\Program Files\Microsoft SDKs\Azure\CLI2\wbin"
if os.path.isdir(_AZ_DIR) and _AZ_DIR not in os.environ.get("PATH", ""):
    os.environ["PATH"] = _AZ_DIR + os.pathsep + os.environ.get("PATH", "")

sys.path.insert(0, ".")

from app.services.databricks_rest import DatabricksUCClient
from app.services.databricks_rest import _dbx_token
from app.services import fabric_rest

# ===================== CONFIGURATION (env-driven) =====================
# Set these in .env or the shell before running:
#   DBX_WORKSPACE_URL, UC_CATALOG, FABRIC_WORKSPACE_ID, UC_TEST_PRINCIPAL
DBX_URL = os.environ["DBX_WORKSPACE_URL"]
UC_CATALOG = os.environ["UC_CATALOG"]
UC_SCHEMA = f"{UC_CATALOG}.sales"
UC_TABLE = f"{UC_CATALOG}.sales.customers"
FABRIC_WORKSPACE_ID = os.environ["FABRIC_WORKSPACE_ID"]
TEST_PRINCIPAL = os.environ["UC_TEST_PRINCIPAL"]
# ======================================================================

# Full privilege sets per securable type (from official Databricks docs, March 2026)
# https://docs.databricks.com/en/data-governance/unity-catalog/manage-privileges/privileges.html
PRIVILEGES_BY_SCOPE = {
    "catalog": [
        "ALL_PRIVILEGES",
        "APPLY_TAG",
        "BROWSE",
        "CREATE_SCHEMA",
        "USE_CATALOG",
        "CREATE_FUNCTION",
        "CREATE_TABLE",
        "CREATE_MATERIALIZED_VIEW",
        "CREATE_MODEL",
        "CREATE_VOLUME",
        "EXTERNAL_USE_SCHEMA",
        "READ_VOLUME",
        "REFRESH",
        "WRITE_VOLUME",
        "EXECUTE",
        "MANAGE",
        "MODIFY",
        "SELECT",
        "USE_SCHEMA",
    ],
    "schema": [
        "ALL_PRIVILEGES",
        "APPLY_TAG",
        "CREATE_FUNCTION",
        "CREATE_TABLE",
        "CREATE_MODEL",
        "CREATE_VOLUME",
        "CREATE_MATERIALIZED_VIEW",
        "MANAGE",
        "EXTERNAL_USE_SCHEMA",
        "USE_SCHEMA",
        "EXECUTE",
        "MODIFY",
        "READ_VOLUME",
        "REFRESH",
        "SELECT",
        "WRITE_VOLUME",
    ],
    "table": [
        "ALL_PRIVILEGES",
        "APPLY_TAG",
        "MANAGE",
        "MODIFY",
        "SELECT",
    ],
}

# Known Fabric mirror mapping (to be validated/updated by this test)
# Fabric mirrored DBX catalogs are READ-ONLY — no writes allowed.
FABRIC_MIRROR_MAPPING = {
    "SELECT":                   {"fabric": "Viewer role / DAR Read",  "supported": True,  "notes": "Core read access — maps to workspace Viewer or OneLake DAR Read"},
    "MODIFY":                   {"fabric": "DAR Write (blocked)",     "supported": False, "notes": "Mirror is read-only; Write DAR fails on mirrored catalog"},
    "ALL_PRIVILEGES":           {"fabric": "Admin role",              "supported": True,  "notes": "Maps to workspace Admin; but write subset is ineffective on mirror"},
    "USE_CATALOG":              {"fabric": "Viewer role (implicit)",  "supported": True,  "notes": "Prerequisite for object access; maps to minimum Viewer role"},
    "USE_SCHEMA":               {"fabric": "Viewer role (implicit)",  "supported": True,  "notes": "Prerequisite for object access; no direct DAR equivalent"},
    "CREATE_SCHEMA":            {"fabric": "Contributor role",        "supported": False, "notes": "Cannot create schemas in a mirrored catalog (read-only)"},
    "CREATE_TABLE":             {"fabric": "Contributor role",        "supported": False, "notes": "Cannot create tables in a mirrored catalog (read-only)"},
    "CREATE_FUNCTION":          {"fabric": "Contributor role",        "supported": False, "notes": "Functions not mirrored to Fabric"},
    "CREATE_MODEL":             {"fabric": "N/A",                     "supported": False, "notes": "ML models not mirrored to Fabric"},
    "CREATE_VOLUME":            {"fabric": "N/A",                     "supported": False, "notes": "Volumes not mirrored to Fabric"},
    "CREATE_MATERIALIZED_VIEW": {"fabric": "N/A",                     "supported": False, "notes": "MVs not mirrored to Fabric"},
    "APPLY_TAG":                {"fabric": "N/A",                     "supported": False, "notes": "Unity Catalog tags not synced to Fabric metadata"},
    "BROWSE":                   {"fabric": "N/A",                     "supported": False, "notes": "Discovery-only privilege; no data access; no Fabric equivalent"},
    "EXECUTE":                  {"fabric": "N/A",                     "supported": False, "notes": "Function execution; functions not mirrored"},
    "READ_VOLUME":              {"fabric": "N/A",                     "supported": False, "notes": "Volumes not mirrored to Fabric"},
    "WRITE_VOLUME":             {"fabric": "N/A",                     "supported": False, "notes": "Volumes not mirrored to Fabric"},
    "REFRESH":                  {"fabric": "N/A",                     "supported": False, "notes": "Materialized view refresh; MVs not mirrored"},
    "MANAGE":                   {"fabric": "Admin role",              "supported": False, "notes": "MANAGE != data access; ownership/admin — no direct Fabric equivalent"},
    "EXTERNAL_USE_SCHEMA":      {"fabric": "N/A",                     "supported": False, "notes": "External engine access via Iceberg REST; not a Fabric concern"},
}


@dataclass
class TestResult:
    securable_type: str
    securable_name: str
    privilege: str
    grant_ok: bool
    grant_verified: bool
    grant_error: str = ""
    fabric_mapping: str = ""
    fabric_supported: bool = False
    notes: str = ""


def get_principal_grants(client: DatabricksUCClient, sec_type: str,
                         full_name: str, principal: str) -> List[str]:
    grants = client.get_grants(sec_type, full_name)
    for g in grants:
        if (g.get("principal") or "").lower() == principal.lower():
            return g.get("privileges", []) or []
    return []


def revoke_all(client: DatabricksUCClient, sec_type: str, full_name: str,
               principal: str) -> List[str]:
    existing = get_principal_grants(client, sec_type, full_name, principal)
    if existing:
        client.update_grants(sec_type, full_name, [
            {"principal": principal, "remove": existing}
        ])
        time.sleep(0.5)
    return existing


def grant_single(client: DatabricksUCClient, sec_type: str, full_name: str,
                 principal: str, privilege: str) -> Tuple[bool, str]:
    try:
        client.update_grants(sec_type, full_name, [
            {"principal": principal, "add": [privilege]}
        ])
        return True, ""
    except httpx.HTTPStatusError as e:
        # Real API rejection — don't retry
        return False, str(e)
    # Let auth/token errors propagate so outer retry loop catches them


def run_test_suite(sec_type: str, full_name: str, privileges: List[str],
                   client: DatabricksUCClient) -> List[TestResult]:
    results = []

    print(f"\n{'='*70}")
    print(f"  Testing {sec_type}: {full_name}")
    print(f"  Privileges to test: {len(privileges)}")
    print(f"{'='*70}\n")

    # Save existing grants
    print("  Saving existing grants...")
    saved_grants = get_principal_grants(client, sec_type, full_name, TEST_PRINCIPAL)
    print(f"    Current: {saved_grants}")

    # Revoke all
    print("  Revoking all grants...")
    revoke_all(client, sec_type, full_name, TEST_PRINCIPAL)
    time.sleep(0.5)

    # Test each privilege
    for i, priv in enumerate(privileges, 1):
        sys.stdout.write(f"  [{i:2d}/{len(privileges)}] {priv:<30s} ")
        sys.stdout.flush()

        # Retry wrapper — up to 3 attempts per privilege in case of token expiry
        for attempt in range(3):
            try:
                # Ensure clean slate
                revoke_all(client, sec_type, full_name, TEST_PRINCIPAL)
                time.sleep(0.3)

                # Grant
                ok, err = grant_single(client, sec_type, full_name, TEST_PRINCIPAL, priv)
                if not ok:
                    short_err = err[:100].split("\n")[0] if err else "unknown"
                    print(f"GRANT FAILED  ({short_err})")
                    mapping = FABRIC_MIRROR_MAPPING.get(priv, {})
                    results.append(TestResult(
                        securable_type=sec_type, securable_name=full_name,
                        privilege=priv, grant_ok=False, grant_verified=False,
                        grant_error=short_err,
                        fabric_mapping=mapping.get("fabric", "N/A"),
                        fabric_supported=mapping.get("supported", False),
                        notes=mapping.get("notes", ""),
                    ))
                    break  # Move to next privilege

                time.sleep(0.3)

                # Verify
                actual = get_principal_grants(client, sec_type, full_name, TEST_PRINCIPAL)
                verified = priv in actual
                mapping = FABRIC_MIRROR_MAPPING.get(priv, {})

                status = "granted+verified" if verified else f"granted (actual={actual})"
                fab = mapping.get("fabric", "?")
                sup = "supported" if mapping.get("supported") else "no-mirror"
                print(f"{status:<28s}  Fabric: {fab:<28s} [{sup}]")

                results.append(TestResult(
                    securable_type=sec_type, securable_name=full_name,
                    privilege=priv, grant_ok=True, grant_verified=verified,
                    fabric_mapping=mapping.get("fabric", "unknown"),
                    fabric_supported=mapping.get("supported", False),
                    notes=mapping.get("notes", ""),
                ))
                break  # Success — move to next privilege

            except Exception as e:
                if attempt < 2:
                    sys.stdout.write(f"\n    [retry {attempt+1}] {str(e)[:80]}... ")
                    sys.stdout.flush()
                    time.sleep(3 * (attempt + 1))  # 3s, 6s backoff
                else:
                    print(f"ERROR after 3 attempts: {str(e)[:80]}")
                    mapping = FABRIC_MIRROR_MAPPING.get(priv, {})
                    results.append(TestResult(
                        securable_type=sec_type, securable_name=full_name,
                        privilege=priv, grant_ok=False, grant_verified=False,
                        grant_error=f"Exception: {str(e)[:80]}",
                        fabric_mapping=mapping.get("fabric", "N/A"),
                        fabric_supported=mapping.get("supported", False),
                        notes=mapping.get("notes", ""),
                    ))

    # Restore
    print(f"\n  Restoring original grants: {saved_grants}")
    revoke_all(client, sec_type, full_name, TEST_PRINCIPAL)
    if saved_grants:
        try:
            client.update_grants(sec_type, full_name, [
                {"principal": TEST_PRINCIPAL, "add": saved_grants}
            ])
        except Exception as e:
            print(f"    RESTORE FAILED: {e}")

    return results


def print_matrix(all_results: List[TestResult]):
    print(f"\n\n{'='*120}")
    print("  UC PRIVILEGE -> FABRIC MIRROR COMPATIBILITY MATRIX")
    print(f"{'='*120}\n")

    for scope in ["catalog", "schema", "table"]:
        scope_results = [r for r in all_results if r.securable_type == scope]
        if not scope_results:
            continue

        name = scope_results[0].securable_name
        print(f"\n  {scope.upper()} ({name})")
        print(f"  {'Privilege':<30s} {'Grantable':<12s} {'Fabric Equivalent':<30s} "
              f"{'Mirror OK':<12s} Notes")
        print(f"  {'-'*110}")

        for r in scope_results:
            g = "Yes" if r.grant_ok else "No"
            s = "Yes" if r.fabric_supported else "No"
            if r.grant_error:
                notes = f"Error: {r.grant_error[:50]}"
            else:
                notes = r.notes[:60]
            print(f"  {r.privilege:<30s} {g:<12s} {r.fabric_mapping:<30s} "
                  f"{s:<12s} {notes}")

    # Summary
    grantable = sum(1 for r in all_results if r.grant_ok)
    supported = sum(1 for r in all_results if r.grant_ok and r.fabric_supported)
    unsupported = sum(1 for r in all_results if r.grant_ok and not r.fabric_supported)
    ungrantable = sum(1 for r in all_results if not r.grant_ok)

    print(f"\n{'='*120}")
    print(f"  SUMMARY")
    print(f"{'='*120}")
    print(f"  Total privileges tested:               {len(all_results)}")
    print(f"  Grantable via UC API:                  {grantable}")
    print(f"  With Fabric mirror support:            {supported}")
    print(f"  No Fabric mirror support (gap):        {unsupported}")
    print(f"  Not grantable on this scope:           {ungrantable}")
    print(f"\n  Fabric mirroring is READ-ONLY. Only SELECT/USE_*/ALL_PRIVILEGES")
    print(f"  have meaningful Fabric equivalents. Write/create/admin privileges")
    print(f"  are grantable in UC but have no effect on the Fabric mirror.\n")


def main():
    print("=" * 70)
    print("  UC PRIVILEGE -> FABRIC MIRROR SYSTEMATIC TEST")
    print("  Testing each privilege individually against the UC API")
    print("=" * 70)

    client = DatabricksUCClient(DBX_URL)

    # Verify connectivity
    print("\nVerifying DBX connectivity...")
    try:
        grants = client.get_grants("catalog", UC_CATALOG)
        print(f"  Catalog {UC_CATALOG}: {len(grants)} principal assignments")
    except Exception as e:
        print(f"  FAILED: {e}")
        sys.exit(1)

    print("Verifying Fabric connectivity...")
    try:
        roles = fabric_rest.list_role_assignments(FABRIC_WORKSPACE_ID)
        print(f"  Workspace: {len(roles)} role assignments")
    except Exception as e:
        print(f"  WARNING: Fabric check failed ({e}) -- continuing with UC tests only")

    all_results: List[TestResult] = []

    test_plan = [
        ("catalog", UC_CATALOG,  PRIVILEGES_BY_SCOPE["catalog"]),
        ("schema",  UC_SCHEMA,   PRIVILEGES_BY_SCOPE["schema"]),
        ("table",   UC_TABLE,    PRIVILEGES_BY_SCOPE["table"]),
    ]

    for sec_type, full_name, privs in test_plan:
        try:
            # Pre-warm token before each scope
            print(f"\n  Pre-warming DBX token...")
            _dbx_token()
            all_results.extend(
                run_test_suite(sec_type, full_name, privs, client))
        except Exception as e:
            print(f"\n  ERROR in {sec_type} suite: {e}")
            print(f"  Continuing with next scope...")

    print_matrix(all_results)

    # Save JSON
    out = []
    for r in all_results:
        out.append({
            "securable_type": r.securable_type,
            "securable_name": r.securable_name,
            "privilege": r.privilege,
            "grant_ok": r.grant_ok,
            "grant_verified": r.grant_verified,
            "grant_error": r.grant_error,
            "fabric_mapping": r.fabric_mapping,
            "fabric_supported": r.fabric_supported,
            "notes": r.notes,
        })
    os.makedirs("scripts", exist_ok=True)
    with open("scripts/uc_privilege_test_results.json", "w") as f:
        json.dump(out, f, indent=2)
    print(f"Results saved to scripts/uc_privilege_test_results.json")


if __name__ == "__main__":
    main()
