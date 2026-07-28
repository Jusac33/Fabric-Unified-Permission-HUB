"""Compute a side-by-side permission diff for a given pairing.

Normalizes both sides to a canonical access class so rows can be compared.
Keys the union by principal, scope, access class, and optional fine-grained
constraint identity.
"""
from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
import hashlib
import json
from typing import Dict, List, Optional, Tuple

import httpx

from app.config import settings
from app.services.databricks_rest import DatabricksUCClient
from app.services import fabric_rest
from app.services import cache as _cache
from app.services.fine_grained_policy import (
    build_fabric_column_constraint,
    build_fabric_row_constraint,
    canonical_column_constraint_key,
    canonical_row_filter_key,
    function_param_names,
)
from app.services import abac_resolver

# Minimal canonical mapping — keep in sync with permission-sync/config/privilege_matrix.yaml
UC_TO_ACCESS = {
    "SELECT": "DATA_READ",
    "MODIFY": "DATA_WRITE",
    "APPLY_TAG": "DATA_ADMIN",
    "ALL_PRIVILEGES": "DATA_ADMIN",
    "USE_CATALOG": "OBJECT_USE",
    "USE_SCHEMA": "OBJECT_USE",
    "CREATE_SCHEMA": "DATA_WRITE",
    "CREATE_TABLE": "DATA_WRITE",
    "CREATE_FUNCTION": "DATA_WRITE",
    "EXECUTE": "OBJECT_USE",
    "READ_VOLUME": "DATA_READ",
    "WRITE_VOLUME": "DATA_WRITE",
}

FABRIC_ROLE_TO_ACCESS = {
    "Viewer": "DATA_READ",
    "Contributor": "DATA_WRITE",
    "Member": "DATA_WRITE",
    "Admin": "DATA_ADMIN",
}

ACCESS_ORDER = ["DATA_READ", "DATA_WRITE", "DATA_ADMIN", "OBJECT_USE"]

# Higher access subsumes lower: Admin covers Write+Read; Contributor covers Read.
ACCESS_SUBSUMES: Dict[str, List[str]] = {
    "DATA_ADMIN": ["DATA_WRITE", "DATA_READ"],
    "DATA_WRITE": ["DATA_READ"],
}

TABLE_POLICY_PRINCIPAL = "__table_policy__"
TABLE_POLICY_DISPLAY = "Table policy"


@dataclass
class DiffRow:
    principal_key: str           # upn / email / object id (lowercased)
    principal_display: str
    principal_type: str          # User | Group | ServicePrincipal | Unknown
    securable_scope: str         # "catalog:<name>" | "schema:<name>.<s>" | "table:<fqn>" | "workspace:<id>"
    access_class: str
    on_dbx: bool = False
    on_fabric: bool = False
    raw_dbx: List[str] = field(default_factory=list)
    raw_fabric: List[str] = field(default_factory=list)
    constraint_kind: str = ""          # "" | "row_filter" | "column_mask"
    constraint_key: str = ""
    constraint_details: dict = field(default_factory=dict)
    has_row_col_diff: bool = False

    @property
    def status(self) -> str:
        if self.on_dbx and not self.on_fabric:
            return "dbx_only"
        if self.on_fabric and not self.on_dbx:
            return "fabric_only"
        return "in_sync"

    @property
    def selection_key(self) -> str:
        return "|".join((
            self.principal_key,
            self.securable_scope,
            self.access_class,
            self.constraint_kind,
            self.constraint_key,
        ))


def _row_identity(row: DiffRow) -> Tuple[str, str, str, str, str]:
    return (
        row.principal_key,
        row.securable_scope,
        row.access_class,
        row.constraint_kind,
        row.constraint_key,
    )


def _constraint_signature(value: object) -> str:
    serialized = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:16]


def _function_name(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("function_name", "functionName", "full_name", "fullName", "name", "function"):
            raw = value.get(key)
            if raw:
                return str(raw)
    return ""


def _input_columns(value: object) -> List[str]:
    if not isinstance(value, dict):
        return []
    for key in (
        "input_column_names",
        "inputColumnNames",
        "input_columns",
        "inputColumns",
        "using_columns",
        "usingColumns",
        "columns",
    ):
        raw = value.get(key)
        if isinstance(raw, list):
            return [str(v) for v in raw]
    return []


def _table_scope(catalog: str, schema: str, table: str) -> str:
    return f"table:{catalog}.{schema}.{table}"


def _record_constraint_row(
    rows: Dict[Tuple[str, str, str, str, str], DiffRow],
    *,
    side: str,
    principal_key: str,
    principal_display: str,
    principal_type: str,
    securable_scope: str,
    constraint_kind: str,
    constraint_key: str,
    raw_label: str,
    details: dict,
) -> None:
    row = DiffRow(
        principal_key=principal_key,
        principal_display=principal_display,
        principal_type=principal_type,
        securable_scope=securable_scope,
        access_class="DATA_READ",
        constraint_kind=constraint_kind,
        constraint_key=constraint_key,
        constraint_details=details,
        has_row_col_diff=True,
    )
    if side == "dbx":
        row.on_dbx = True
        row.raw_dbx.append(raw_label)
    else:
        row.on_fabric = True
        row.raw_fabric.append(raw_label)

    key = _row_identity(row)
    existing = rows.get(key)
    if not existing:
        rows[key] = row
        return
    existing.on_dbx = existing.on_dbx or row.on_dbx
    existing.on_fabric = existing.on_fabric or row.on_fabric
    # Preserve ABAC provenance when a resolved policy target dedupes into a
    # constraint already discovered via the table's effective masks/filters.
    # The Fabric constraint is identical; we just annotate that a governed-tag
    # ABAC policy drives it so the UI can identify it.
    if details.get("source") == "databricks_abac_resolved":
        existing.constraint_details.setdefault("abac_policy", details.get("policy_name"))
        existing.constraint_details.setdefault("abac_policy_id", details.get("policy_id"))
        existing.constraint_details["abac_driven"] = True
    for raw in row.raw_dbx:
        if raw not in existing.raw_dbx:
            existing.raw_dbx.append(raw)
    for raw in row.raw_fabric:
        if raw not in existing.raw_fabric:
            existing.raw_fabric.append(raw)


def _dbx_principal_key(assignment: dict) -> Tuple[str, str, str]:
    """UC permission API returns: {'principal': '<email|group|sp-appid>', 'privileges': [...]}"""
    principal = (assignment.get("principal") or "").strip()
    key = principal.lower()
    # crude type inference
    if "@" in principal:
        ptype = "User"
    elif principal.count("-") == 4 and len(principal) == 36:
        ptype = "ServicePrincipal"
    else:
        ptype = "Group"
    return key, principal, ptype


def _fabric_principal_key(ra: dict) -> Tuple[str, str, str]:
    pr = ra.get("principal") or {}
    disp = pr.get("displayName") or pr.get("userPrincipalName") or pr.get("id", "")
    oid = pr.get("id") or ""
    upn = pr.get("userPrincipalName") or ""
    # Prefer OID as key — it matches Graph-resolved DBX principals.
    # Fall back to UPN for email matching, then displayName.
    key = (oid or upn or disp).lower()
    return key, disp, pr.get("type") or "Unknown"


def collect_dbx_rows(dbx_url: str, catalog: str) -> List[DiffRow]:
    rows: Dict[Tuple[str, str, str, str, str], DiffRow] = {}
    client = DatabricksUCClient(dbx_url)

    def _record(securable_scope: str, grants: List[dict]) -> None:
        for g in grants:
            pkey, pdisp, ptype = _dbx_principal_key(g)
            if not pkey:
                continue
            for priv in g.get("privileges", []) or []:
                access = UC_TO_ACCESS.get(priv.upper())
                if not access:
                    continue
                k = (pkey, securable_scope, access, "", "")
                row = rows.get(k) or DiffRow(
                    principal_key=pkey, principal_display=pdisp,
                    principal_type=ptype, securable_scope=securable_scope,
                    access_class=access)
                row.on_dbx = True
                if priv not in row.raw_dbx:
                    row.raw_dbx.append(priv)
                rows[k] = row

    # catalog grants
    _record(f"catalog:{catalog}", client.get_grants("catalog", catalog))

    # Collect ABAC (attribute-based) policies across all scopes for resolution.
    all_policies: List[dict] = list(client.list_policies("catalog", catalog))

    schemas = client.list_schemas(catalog)

    # Fetch schema grants + tables in parallel (bounded)
    schemas = [s for s in schemas[:50]
               if s.get("name") and s.get("name") != "information_schema"]

    def _fetch_schema(s):
        sname = s.get("name", "")
        fqn = f"{catalog}.{sname}"
        out = {"scope": f"schema:{fqn}", "grants": [],
               "tables": [], "sname": sname, "policies": []}
        out["grants"] = client.get_grants("schema", fqn)
        out["tables"] = client.list_tables(catalog, sname)[:50]
        out["policies"] = client.list_policies("schema", fqn)
        return out

    with ThreadPoolExecutor(max_workers=12) as ex:
        schema_results = list(ex.map(_fetch_schema, schemas))

    for sr in schema_results:
        _record(sr["scope"], sr["grants"])
        all_policies.extend(sr["policies"])

    # Now fetch all table grants in parallel
    table_jobs: List[Tuple[str, str]] = []
    for sr in schema_results:
        for t in sr["tables"]:
            tname = t.get("name", "")
            if not tname:
                continue
            tfqn = f"{catalog}.{sr['sname']}.{tname}"
            table_jobs.append((f"table:{tfqn}", tfqn))

    function_cache: dict[str, dict] = {}

    def _get_function(function_name: str) -> dict:
        if not function_name:
            return {}
        if function_name not in function_cache:
            try:
                function_cache[function_name] = client.get_function(function_name)
            except (AttributeError, httpx.HTTPStatusError):
                function_cache[function_name] = {}
        return function_cache[function_name]

    def _fetch_table_data(job):
        scope, fqn = job
        metadata = {}
        try:
            metadata = client.get_table(fqn)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 404:
                raise
        policies = client.list_policies("table", fqn)
        return scope, fqn, client.get_grants("table", fqn), metadata, policies

    # Maps used to materialize ABAC policies onto concrete tables/columns.
    tables_by_schema: Dict[str, List[str]] = {}
    columns_by_table: Dict[Tuple[str, str], List[str]] = {}

    if table_jobs:
        with ThreadPoolExecutor(max_workers=24) as ex:
            for scope, fqn, grants, metadata, policies in ex.map(_fetch_table_data, table_jobs):
                _record(scope, grants)
                _record_uc_constraints(
                    rows,
                    catalog,
                    scope,
                    fqn,
                    metadata,
                    function_resolver=_get_function,
                )
                all_policies.extend(policies)
                parts = fqn.split(".")
                if len(parts) >= 3:
                    sname, tname = parts[1], parts[2]
                    tables_by_schema.setdefault(sname, []).append(tname)
                    columns_by_table[(sname, tname)] = [
                        str(col.get("name") or "")
                        for col in metadata.get("columns") or []
                        if isinstance(col, dict) and col.get("name")
                    ]

    _record_abac(
        rows,
        client,
        catalog,
        all_policies,
        tables_by_schema,
        columns_by_table,
        function_resolver=_get_function,
    )

    return list(rows.values())


def _record_uc_constraints(
    rows: Dict[Tuple[str, str, str, str, str], DiffRow],
    catalog: str,
    scope: str,
    fqn: str,
    metadata: dict,
    function_resolver=None,
) -> None:
    if not metadata:
        return
    parts = fqn.split(".")
    table_name = parts[-1] if parts else fqn
    schema_name = parts[-2] if len(parts) >= 2 else ""
    table_scope = scope if scope.startswith("table:") else _table_scope(catalog, schema_name, table_name)

    row_filters: List[object] = []
    for key in ("row_filters", "rowFilters"):
        raw = metadata.get(key)
        if isinstance(raw, list):
            row_filters.extend(raw)
        elif isinstance(raw, dict):
            nested = raw.get("row_filters") or raw.get("rowFilters")
            if isinstance(nested, list):
                row_filters.extend(nested)
    for key in ("row_filter", "rowFilter"):
        raw = metadata.get(key)
        if isinstance(raw, dict):
            nested = raw.get("row_filters") or raw.get("rowFilters")
            if isinstance(nested, list):
                row_filters.extend(nested)
            else:
                row_filters.append(raw)
        elif raw:
            row_filters.append(raw)

    for row_filter in row_filters:
        function_name = _function_name(row_filter)
        function_info = function_resolver(function_name) if function_resolver else {}
        input_columns = _input_columns(row_filter) or function_param_names(function_info)
        details = {
            "table": fqn,
            "function": function_name,
            "input_columns": input_columns,
            "function_definition": function_info.get("routine_definition"),
            "source": "databricks",
            "raw": row_filter,
        }
        constraint_key = _constraint_signature({"uc_row_filter": details})
        try:
            translated = build_fabric_row_constraint(
                table_scope,
                catalog,
                function_info,
                input_columns,
                allow_test_degraded=not settings.is_production,
            )
            details["predicate"] = translated.predicate
            if translated.note:
                details["translation_note"] = translated.note
            constraint_key = canonical_row_filter_key(table_scope, translated.predicate)
        except ValueError as exc:
            details["translation_error"] = str(exc)
        _record_constraint_row(
            rows,
            side="dbx",
            principal_key=TABLE_POLICY_PRINCIPAL,
            principal_display=TABLE_POLICY_DISPLAY,
            principal_type="Policy",
            securable_scope=table_scope,
            constraint_kind="row_filter",
            constraint_key=constraint_key,
            raw_label=f"ROW FILTER {function_name or '(unknown)'}",
            details=details,
        )

    table_columns = [
        str(column.get("name") or "")
        for column in metadata.get("columns") or []
        if isinstance(column, dict) and column.get("name")
    ]
    for column in metadata.get("columns") or []:
        if not isinstance(column, dict):
            continue
        column_name = str(column.get("name") or "")
        masks: List[object] = []
        for key in ("mask", "column_mask", "columnMask"):
            raw = column.get(key)
            if raw:
                masks.append(raw)
        for key in ("effective_masks", "effectiveMasks"):
            raw = column.get(key)
            if isinstance(raw, list):
                masks.extend(raw)
        for mask in masks:
            function_name = _function_name(mask)
            function_info = function_resolver(function_name) if function_resolver else {}
            details = {
                "table": fqn,
                "column": column_name,
                "function": function_name,
                "input_columns": _input_columns(mask) or function_param_names(function_info),
                "function_definition": function_info.get("routine_definition"),
                "table_columns": table_columns,
                "source": "databricks",
                "raw": mask,
            }
            constraint_key = _constraint_signature({"uc_column_mask": details})
            try:
                translated = build_fabric_column_constraint(
                    table_scope,
                    catalog,
                    column_name,
                    table_columns,
                )
                details["columns"] = list(translated.column_names)
                if translated.note:
                    details["translation_note"] = translated.note
                constraint_key = canonical_column_constraint_key(table_scope, translated.column_names)
            except ValueError as exc:
                details["translation_error"] = str(exc)
            _record_constraint_row(
                rows,
                side="dbx",
                principal_key=TABLE_POLICY_PRINCIPAL,
                principal_display=TABLE_POLICY_DISPLAY,
                principal_type="Policy",
                securable_scope=table_scope,
                constraint_kind="column_mask",
                constraint_key=constraint_key,
                raw_label=f"COLUMN MASK {column_name}",
                details=details,
            )


def _abac_scope(on_type: str, full_name: str) -> str:
    mapping = {"CATALOG": "catalog", "SCHEMA": "schema", "TABLE": "table"}
    prefix = mapping.get((on_type or "").upper())
    if not prefix or not full_name:
        return ""
    return f"{prefix}:{full_name}"


def _abac_principals(policy: dict) -> List[str]:
    to_principals = policy.get("to_principals") or policy.get("toPrincipals") or []
    return [str(p) for p in to_principals if p]


def _record_abac_policies(
    rows: Dict[Tuple[str, str, str, str, str], DiffRow],
    policies: List[dict],
) -> None:
    """Surface Unity Catalog ABAC (attribute-based) policies as fine-grained rows.

    ABAC policies attach a row-filter or column-mask to a securable and apply
    dynamically to any object carrying the targeted governed tag. Fabric OneLake
    has no tag-driven equivalent, so this read-path records each policy at its
    attachment scope with the tag condition captured for review. It does not
    materialize the policy onto every dynamically-matched table.
    """
    for policy in policies or []:
        if not isinstance(policy, dict):
            continue
        scope = _abac_scope(
            policy.get("on_securable_type") or policy.get("onSecurableType") or "",
            policy.get("on_securable_fullname") or policy.get("onSecurableFullname") or "",
        )
        if not scope:
            continue

        column_mask = policy.get("column_mask") or policy.get("columnMask")
        row_filter = policy.get("row_filter") or policy.get("rowFilter")
        policy_type = str(policy.get("policy_type") or policy.get("policyType") or "").upper()
        if column_mask or "COLUMN_MASK" in policy_type:
            constraint_kind = "column_mask"
            spec = column_mask if isinstance(column_mask, dict) else {}
        elif row_filter or "ROW_FILTER" in policy_type:
            constraint_kind = "row_filter"
            spec = row_filter if isinstance(row_filter, dict) else {}
        else:
            continue

        policy_id = str(policy.get("id") or policy.get("name") or "")
        function_name = _function_name(spec)
        when_condition = (
            policy.get("when_condition")
            or policy.get("whenCondition")
            or policy.get("condition")
            or ""
        )
        details = {
            "source": "databricks_abac",
            "policy_id": policy_id,
            "policy_name": policy.get("name"),
            "comment": policy.get("comment"),
            "function": function_name,
            "on_column": spec.get("on_column") or spec.get("onColumn"),
            "match_columns": policy.get("match_columns") or policy.get("matchColumns"),
            "when_condition": when_condition,
            "to_principals": _abac_principals(policy),
            "except_principals": policy.get("except_principals")
            or policy.get("exceptPrincipals")
            or [],
            "attached_scope": scope,
            "raw": policy,
            # Tag-driven dynamic policy: no static Fabric equivalent exists.
            "translation_error": "ABAC tag-driven policy has no Fabric OneLake equivalent",
        }
        constraint_key = _constraint_signature(
            {"uc_abac_policy": policy_id or details}
        )
        label_kind = "COLUMN MASK" if constraint_kind == "column_mask" else "ROW FILTER"
        raw_label = f"ABAC {label_kind} {policy.get('name') or function_name or '(unnamed)'}"
        _record_constraint_row(
            rows,
            side="dbx",
            principal_key=TABLE_POLICY_PRINCIPAL,
            principal_display=TABLE_POLICY_DISPLAY,
            principal_type="Policy",
            securable_scope=scope,
            constraint_kind=constraint_kind,
            constraint_key=constraint_key,
            raw_label=raw_label,
            details=details,
        )


def _record_abac(
    rows: Dict[Tuple[str, str, str, str, str], DiffRow],
    client,
    catalog: str,
    policies: List[dict],
    tables_by_schema: Dict[str, List[str]],
    columns_by_table: Dict[Tuple[str, str], List[str]],
    function_resolver=None,
) -> None:
    """Surface ABAC policies in the diff.

    When a SQL warehouse is configured (``DBX_WAREHOUSE_ID``) we read governed
    tags and *materialize* each policy onto the concrete tables/columns its tag
    conditions match, emitting normal row_filter / column_mask rows that the
    apply path can translate to Fabric OneLake DAS. Policies that resolve to no
    targets (or when no warehouse is available) are recorded as abstract,
    review-only rows that are skipped on apply.
    """
    if not policies:
        return

    warehouse_id = (getattr(settings, "DBX_WAREHOUSE_ID", "") or "").strip()
    governed_tags = None
    if warehouse_id:
        try:
            governed_tags = abac_resolver.read_governed_tags(client, warehouse_id, catalog)
        except Exception:  # noqa: BLE001 - tag read is best-effort
            governed_tags = None

    if governed_tags is None:
        # No warehouse / tag read failed — keep policies visible but unresolved.
        _record_abac_policies(rows, policies)
        return

    targets = abac_resolver.resolve_policies(
        policies, governed_tags, tables_by_schema, columns_by_table
    )
    for target in targets:
        _record_resolved_abac_target(rows, catalog, target, columns_by_table, function_resolver)

    # Any policy that matched no concrete target: record an abstract row so it
    # stays visible in the diff (review-only, skipped on apply).
    matched_ids = {t.policy_id for t in targets}
    unresolved = [
        p for p in policies
        if isinstance(p, dict)
        and str(p.get("id") or p.get("name") or "") not in matched_ids
    ]
    if unresolved:
        _record_abac_policies(rows, unresolved)


def _record_resolved_abac_target(
    rows: Dict[Tuple[str, str, str, str, str], DiffRow],
    catalog: str,
    target,
    columns_by_table: Dict[Tuple[str, str], List[str]],
    function_resolver=None,
) -> None:
    table_scope = _table_scope(catalog, target.schema, target.table)
    table_fqn = f"{catalog}.{target.schema}.{target.table}"
    table_columns = columns_by_table.get((target.schema, target.table), [])

    details = {
        "source": "databricks_abac_resolved",
        "policy_id": target.policy_id,
        "policy_name": target.policy_name,
        "function": target.function_name,
        "input_columns": list(target.input_columns),
        "table": table_fqn,
        "table_columns": table_columns,
        "when_condition": target.when_condition,
        "to_principals": target.to_principals,
        "except_principals": target.except_principals,
        "materialized": True,
    }

    if target.constraint_kind == "column_mask":
        details["column"] = target.column
        constraint_key = _constraint_signature({"abac_column_mask": details})
        try:
            translated = build_fabric_column_constraint(
                table_scope, catalog, target.column, table_columns
            )
            details["columns"] = list(translated.column_names)
            if translated.note:
                details["translation_note"] = translated.note
            constraint_key = canonical_column_constraint_key(
                table_scope, translated.column_names
            )
        except ValueError as exc:
            details["translation_error"] = str(exc)
        raw_label = f"ABAC COLUMN MASK {target.policy_name or target.function_name} -> {target.column}"
        _record_constraint_row(
            rows, side="dbx", principal_key=TABLE_POLICY_PRINCIPAL,
            principal_display=TABLE_POLICY_DISPLAY, principal_type="Policy",
            securable_scope=table_scope, constraint_kind="column_mask",
            constraint_key=constraint_key, raw_label=raw_label, details=details,
        )
        return

    # row_filter
    function_info = function_resolver(target.function_name) if function_resolver else {}
    details["function_definition"] = function_info.get("routine_definition")
    constraint_key = _constraint_signature({"abac_row_filter": details})
    try:
        translated = build_fabric_row_constraint(
            table_scope, catalog, function_info, target.input_columns,
            allow_test_degraded=not settings.is_production,
        )
        details["predicate"] = translated.predicate
        if translated.note:
            details["translation_note"] = translated.note
        constraint_key = canonical_row_filter_key(table_scope, translated.predicate)
    except ValueError as exc:
        details["translation_error"] = str(exc)
    raw_label = f"ABAC ROW FILTER {target.policy_name or target.function_name}"
    _record_constraint_row(
        rows, side="dbx", principal_key=TABLE_POLICY_PRINCIPAL,
        principal_display=TABLE_POLICY_DISPLAY, principal_type="Policy",
        securable_scope=table_scope, constraint_kind="row_filter",
        constraint_key=constraint_key, raw_label=raw_label, details=details,
    )


DAR_ACTION_TO_ACCESS = {
    # Canonical FabricPolicyAccessType values
    "Read": "DATA_READ",
    "ReadAll": "DATA_READ",
    "Write": "DATA_WRITE",
    "Reshare": "DATA_ADMIN",
    "Execute": "OBJECT_USE",
    "Explore": "OBJECT_USE",
    # Legacy long-form names (defensive)
    "Microsoft.Fabric/OneLake/Read": "DATA_READ",
    "Microsoft.Fabric/OneLake/Write": "DATA_WRITE",
}


def _mirrored_item_for(workspace_id: str, catalog: str) -> Optional[dict]:
    items = fabric_rest.list_items(workspace_id)
    candidates = [i for i in items if i.get("type") == "MirroredAzureDatabricksCatalog"]
    for it in candidates:
        if (it.get("displayName") or "").lower() == (catalog or "").lower():
            return it
    return None


def collect_fabric_rows(workspace_id: str, uc_catalog: str = "") -> List[DiffRow]:
    rows: Dict[Tuple[str, str, str, str, str], DiffRow] = {}

    # --- Layer 1: workspace role assignments ---
    try:
        assignments = fabric_rest.list_role_assignments(workspace_id)
    except Exception as exc:
        raise RuntimeError(
            "Fabric auth failed – ensure your managed identity / az login has Fabric API access. "
            f"Underlying error: {exc}"
        ) from exc
    ws_scope = f"workspace:{workspace_id}"
    for ra in assignments:
        pkey, pdisp, ptype = _fabric_principal_key(ra)
        if not pkey:
            continue
        role = ra.get("role") or ra.get("roleName", "")
        access = FABRIC_ROLE_TO_ACCESS.get(role)
        if not access:
            continue
        # Emit a workspace-scope row
        k = (pkey, ws_scope, access, "", "")
        row = rows.get(k) or DiffRow(
            principal_key=pkey, principal_display=pdisp,
            principal_type=ptype, securable_scope=ws_scope,
            access_class=access)
        row.on_fabric = True
        if role not in row.raw_fabric:
            row.raw_fabric.append(role)
        rows[k] = row
        # Workspace roles functionally cover the paired catalog.
        # Emit a catalog-scope row so it can match the DBX catalog grant.
        # Higher access subsumes lower (Admin covers DATA_READ + DATA_WRITE).
        if uc_catalog:
            cat_scope = f"catalog:{uc_catalog}"
            access_classes = [access] + ACCESS_SUBSUMES.get(access, [])
            for ac in access_classes:
                ck = (pkey, cat_scope, ac, "", "")
                crow = rows.get(ck) or DiffRow(
                    principal_key=pkey, principal_display=pdisp,
                    principal_type=ptype, securable_scope=cat_scope,
                    access_class=ac)
                crow.on_fabric = True
                if role not in crow.raw_fabric:
                    crow.raw_fabric.append(role)
                rows[ck] = crow
            # Any workspace role also satisfies USE_CATALOG (OBJECT_USE).
            ok = (pkey, cat_scope, "OBJECT_USE", "", "")
            orow = rows.get(ok) or DiffRow(
                principal_key=pkey, principal_display=pdisp,
                principal_type=ptype, securable_scope=cat_scope,
                access_class="OBJECT_USE")
            orow.on_fabric = True
            if role not in orow.raw_fabric:
                orow.raw_fabric.append(role)
            rows[ok] = orow

    # --- Layer 3: OneLake DAR on the mirrored catalog item (if pairing is set) ---
    if uc_catalog:
        item = _mirrored_item_for(workspace_id, uc_catalog)
        if item and item.get("id"):
            dars = fabric_rest.list_data_access_policies(
                workspace_id, item["id"])
            for dar in dars or []:
                members = _dar_members(dar)
                for rule in dar.get("decisionRules") or []:
                    paths = _dar_rule_values(rule, "Path")
                    scopes = _scopes_from_dar_paths(uc_catalog, paths)
                    _record_fabric_constraints(rows, uc_catalog, members, rule)
                    for action in _dar_rule_values(rule, "Action"):
                        access = DAR_ACTION_TO_ACCESS.get(action)
                        if not access:
                            continue
                        for m in members:
                            oid = (m.get("objectId") or "").lower()
                            if not oid:
                                continue
                            pdisp = m.get("objectId") or ""
                            ptype = m.get("objectType") or "User"
                            for sc in scopes:
                                k = (oid, sc, access, "", "")
                                row = rows.get(k) or DiffRow(
                                    principal_key=oid,
                                    principal_display=pdisp,
                                    principal_type=ptype,
                                    securable_scope=sc,
                                    access_class=access,
                                )
                                row.on_fabric = True
                                if action not in row.raw_fabric:
                                    row.raw_fabric.append(action)
                                rows[k] = row
    return list(rows.values())


def _record_fabric_constraints(
    rows: Dict[Tuple[str, str, str, str, str], DiffRow],
    uc_catalog: str,
    members: List[dict],
    rule: dict,
) -> None:
    constraints = rule.get("constraints") or {}
    if not isinstance(constraints, dict):
        return
    for row_constraint in constraints.get("rows") or []:
        if not isinstance(row_constraint, dict):
            continue
        table_path = str(row_constraint.get("tablePath") or "")
        scope = _scope_from_table_path(uc_catalog, table_path)
        predicate = str(row_constraint.get("value") or "")
        details = {
            "table_path": table_path,
            "predicate": predicate,
            "type": row_constraint.get("type"),
            "source": "fabric",
            "raw": row_constraint,
        }
        _record_fabric_constraint_for_members(
            rows,
            members,
            scope,
            "row_filter",
            canonical_row_filter_key(scope, predicate),
            "ROW CONSTRAINT",
            details,
        )

    for column_constraint in constraints.get("columns") or []:
        if not isinstance(column_constraint, dict):
            continue
        table_path = str(column_constraint.get("tablePath") or "")
        scope = _scope_from_table_path(uc_catalog, table_path)
        column_names = [str(c) for c in column_constraint.get("columnNames") or []]
        details = {
            "table_path": table_path,
            "columns": column_names,
            "column_effect": column_constraint.get("columnEffect"),
            "column_action": column_constraint.get("columnAction") or [],
            "source": "fabric",
            "raw": column_constraint,
        }
        _record_fabric_constraint_for_members(
            rows,
            members,
            scope,
            "column_mask",
            canonical_column_constraint_key(scope, column_names),
            "COLUMN CONSTRAINT",
            details,
        )


def _record_fabric_constraint_for_members(
    rows: Dict[Tuple[str, str, str, str, str], DiffRow],
    members: List[dict],
    scope: str,
    constraint_kind: str,
    constraint_key: str,
    raw_label: str,
    details: dict,
) -> None:
    details = dict(details)
    details["members"] = members
    _record_constraint_row(
        rows,
        side="fabric",
        principal_key=TABLE_POLICY_PRINCIPAL,
        principal_display=TABLE_POLICY_DISPLAY,
        principal_type="Policy",
        securable_scope=scope,
        constraint_kind=constraint_kind,
        constraint_key=constraint_key,
        raw_label=raw_label,
        details=details,
    )


def _dar_members(dar: dict) -> List[dict]:
    members = dar.get("members") or {}
    return (
        members.get("microsoftEntraMembers")
        or members.get("entraMembers")
        or members.get("microsoftEntraUsers")
        or []
    )


def _dar_rule_values(rule: dict, attribute_name: str) -> List[str]:
    values: List[str] = []
    for container_name in ("permission", "attributes"):
        for attr in rule.get(container_name) or []:
            if attr.get("attributeName") != attribute_name:
                continue
            values.extend(attr.get("attributeValueIncludedIn") or [])
    return values


def _scopes_from_dar_paths(uc_catalog: str, paths: List[str]) -> List[str]:
    scopes: List[str] = []
    for path in paths or ["/"]:
        clean = (path or "").strip()
        if clean in {"", "/", "*"}:
            scopes.append(f"catalog:{uc_catalog}")
            continue

        segs = [segment for segment in clean.strip("/").split("/") if segment]
        if not segs or segs == ["*"]:
            scopes.append(f"catalog:{uc_catalog}")
            continue

        if segs[0].lower() == "tables":
            if len(segs) == 1:
                scopes.append(f"catalog:{uc_catalog}")
            elif len(segs) == 2:
                scopes.append(f"schema:{uc_catalog}.{segs[1]}")
            else:
                scopes.append(f"table:{uc_catalog}.{segs[1]}.{'.'.join(segs[2:])}")
            continue

        if len(segs) == 1:
            scopes.append(f"schema:{uc_catalog}.{segs[0]}")
        else:
            scopes.append(f"table:{uc_catalog}.{segs[0]}.{'.'.join(segs[1:])}")

    return scopes


def _scope_from_table_path(uc_catalog: str, table_path: str) -> str:
    scopes = _scopes_from_dar_paths(uc_catalog, [table_path])
    return scopes[0] if scopes else f"catalog:{uc_catalog}"


def compute_diff(dbx_url: str, uc_catalog: str,
                 fabric_workspace_id: str,
                 use_cache: bool = True) -> Dict[str, List[DiffRow]]:
    cache_key = f"diff::{dbx_url}::{uc_catalog}::{fabric_workspace_id}"
    if use_cache:
        cached_value = _cache.get(cache_key)
        if cached_value is not None:
            return cached_value

    # Run DBX + Fabric collection in parallel — they are independent
    errors: List[str] = []
    with ThreadPoolExecutor(max_workers=2) as ex:
        f_dbx = ex.submit(collect_dbx_rows, dbx_url, uc_catalog)
        f_fab = ex.submit(collect_fabric_rows, fabric_workspace_id, uc_catalog)
        try:
            dbx = f_dbx.result()
        except Exception as exc:
            dbx = []
            errors.append(f"DBX: {exc}")
        try:
            fab = f_fab.result()
        except Exception as exc:
            fab = []
            errors.append(f"Fabric: {exc}")

    # Normalize DBX email principals to Entra OID so they can match Fabric DAR rows.
    # Resolve unique emails in parallel (each row may share an email).
    from app.services.pair_apply import _resolve_entra  # local import to avoid cycle
    email_rows: Dict[str, List[DiffRow]] = {}
    for r in dbx:
        if "@" in r.principal_key:
            email_rows.setdefault(
                (r.principal_display or r.principal_key).lower(), []).append(r)
    # Also collect Fabric rows that have email/UPN keys, so we normalize
    # both sides to OIDs for matching.
    fab_email_rows: Dict[str, List[DiffRow]] = {}
    for r in fab:
        if "@" in r.principal_key:
            fab_email_rows.setdefault(r.principal_key.lower(), []).append(r)
    all_emails = set(email_rows.keys()) | set(fab_email_rows.keys())
    resolved_map: Dict[str, Optional[tuple]] = {}
    if all_emails:
        with ThreadPoolExecutor(max_workers=min(8, len(all_emails))) as ex:
            resolved_map = dict(zip(
                all_emails,
                ex.map(_resolve_entra, all_emails),
            ))
        for email, rs in email_rows.items():
            res = resolved_map.get(email)
            if not res:
                continue
            oid, _ = res
            for r in rs:
                r.principal_key = oid.lower()
        for email, rs in fab_email_rows.items():
            res = resolved_map.get(email)
            if not res:
                continue
            oid, _ = res
            for r in rs:
                r.principal_key = oid.lower()

    merged: Dict[Tuple[str, str, str, str, str], DiffRow] = {}
    for r in dbx + fab:
        k = _row_identity(r)
        if k not in merged:
            merged[k] = r
        else:
            m = merged[k]
            m.on_dbx = m.on_dbx or r.on_dbx
            m.on_fabric = m.on_fabric or r.on_fabric
            for x in r.raw_dbx:
                if x not in m.raw_dbx:
                    m.raw_dbx.append(x)
            for x in r.raw_fabric:
                if x not in m.raw_fabric:
                    m.raw_fabric.append(x)

    all_rows = sorted(
        merged.values(),
        key=lambda r: (
            r.status != "in_sync",
            r.constraint_kind or "",
            r.principal_display,
            r.access_class,
        ),
    )
    result = {
        "all": all_rows,
        "dbx_only": [r for r in all_rows if r.status == "dbx_only"],
        "fabric_only": [r for r in all_rows if r.status == "fabric_only"],
        "in_sync": [r for r in all_rows if r.status == "in_sync"],
        "errors": errors,
    }
    _cache.set(cache_key, result, ttl=300.0)
    return result
