"""Apply selected permission changes for a pairing (dry-run default).

Fabric write layer is auto-selected per scope:
  * catalog-scope / workspace-scope  -> Layer 1 workspace role assignment
  * table-scope                      -> Layer 3 OneLake Data Access Role
                                        on an exact mirrored catalog item match
  * schema-scope                     -> unsupported (no safe Fabric equivalent)
"""
from __future__ import annotations
import hashlib
import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import List, Optional

import httpx

from app.config import settings
from app.services import fabric_rest
from app.services.azure_identity import get_fabric_token, get_token
from app.services.databricks_rest import DatabricksUCClient
from app.services.fine_grained_policy import (
    FabricConstraintTranslation,
    build_fabric_column_constraint,
    build_fabric_row_constraint,
    fabric_table_path,
    normalize_row_predicate,
    referenced_columns,
    table_parts_from_scope,
)

ACCESS_TO_FABRIC_ROLE = {
    "DATA_READ": "Viewer",
    "DATA_WRITE": "Contributor",
    "DATA_ADMIN": "Admin",
    # OBJECT_USE (Unity Catalog USE_CATALOG / USE_SCHEMA) is deliberately absent.
    # It is a traverse privilege: on its own it grants no data access in UC, so
    # turning it into a Fabric workspace Viewer would hand out read over every
    # item in the workspace. It also broke round-trip safety, because both
    # OBJECT_USE and DATA_READ collapsed to "Viewer" while pair_diff maps
    # "Viewer" back to the broader DATA_READ - so USE_CATALOG re-entered Unity
    # Catalog as USE_CATALOG + SELECT.
}

ACCESS_TO_UC_PRIV = {
    "DATA_READ": ["SELECT"],
    "DATA_WRITE": ["MODIFY"],
    "DATA_ADMIN": ["ALL_PRIVILEGES"],
    "OBJECT_USE": ["USE_CATALOG"],
}

UC_PRIV_BY_SCOPE = {
    "catalog": {
        "DATA_READ": ["USE_CATALOG", "SELECT"],
        "DATA_WRITE": ["USE_CATALOG", "MODIFY"],
        "DATA_ADMIN": ["ALL_PRIVILEGES"],
        "OBJECT_USE": ["USE_CATALOG"],
    },
    "schema": {
        "DATA_READ": ["USE_SCHEMA", "SELECT"],
        "DATA_WRITE": ["USE_SCHEMA", "MODIFY"],
        "DATA_ADMIN": ["ALL_PRIVILEGES"],
        "OBJECT_USE": ["USE_SCHEMA"],
    },
    "table": {
        "DATA_READ": ["SELECT"],
        "DATA_WRITE": ["MODIFY"],
        "DATA_ADMIN": ["ALL_PRIVILEGES"],
        "OBJECT_USE": [],
    },
}

ACCESS_TO_DAR_ACTIONS = {
    "DATA_READ": ["Read"],
    "DATA_WRITE": ["Read", "Write"],
    "DATA_ADMIN": ["Read", "Write", "Reshare"],
}

_GUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                   r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


@dataclass
class ApplyAction:
    direction: str
    principal: str
    securable_scope: str
    access_class: str
    target_action: str
    layer: str = ""
    ok: bool = False
    skipped: bool = False
    message: str = ""


@dataclass
class ApplyReport:
    dry_run: bool
    actions: List[ApplyAction] = field(default_factory=list)

    @property
    def n_ok(self) -> int:
        return sum(1 for a in self.actions if a.ok and not a.skipped)

    @property
    def n_skipped(self) -> int:
        return sum(1 for a in self.actions if a.skipped)

    @property
    def n_failed(self) -> int:
        return sum(1 for a in self.actions if not a.ok and not a.skipped)


# DBX-internal principals that have no Entra mapping (account groups, workspace-local groups)
_DBX_INTERNAL_PATTERNS = (
    re.compile(r"^_workspace_users_", re.I),
    re.compile(r"^account users$", re.I),
    re.compile(r"^admins$", re.I),
    re.compile(r"^users$", re.I),
)


def _is_dbx_internal_principal(name: str) -> bool:
    n = (name or "").strip()
    if "@" in n:
        return False
    if _GUID.match(n):
        return False
    return any(p.match(n) for p in _DBX_INTERNAL_PATTERNS)


def _queue_unmapped(pairing: dict, principal: str, principal_type: str, reason: str) -> None:
    """Park an unmapped principal in the identity reconciliation queue."""
    try:
        from app.services import identity_queue
        identity_queue.enqueue(
            pairing_id=pairing.get("id"),
            principal=principal,
            principal_type=principal_type or "",
            source_platform="unity_catalog",
            reason=reason,
        )
    except Exception:
        # Reconciliation is best-effort; never block an apply on queue write.
        pass


@lru_cache(maxsize=1)
def _get_tenant_id_from_token() -> str:
    """Decode the Fabric access token (no signature check) and return its `tid` claim."""
    import base64, json as _json
    try:
        tok = get_fabric_token()
        payload_b64 = tok.split(".")[1] + "==="
        payload = _json.loads(base64.urlsafe_b64decode(payload_b64))
        return payload.get("tid") or ""
    except Exception:
        return ""


# ---------- Graph resolve (UC email -> Entra object ID) ----------
@lru_cache(maxsize=512)
def _resolve_entra(principal: str) -> Optional[tuple[str, str]]:
    """Return (object_id, principal_type) for a UPN/email/displayName, or None."""
    p = (principal or "").strip()
    if not p:
        return None
    if _GUID.match(p):
        return (p, "User")
    try:
        token = get_token("https://graph.microsoft.com/.default")
    except Exception:
        return None
    headers = {"Authorization": f"Bearer {token}"}
    esc = p.replace("'", "''")
    # Try user by UPN/mail, then group, then SP
    queries = [
        ("/users", f"userPrincipalName eq '{esc}' or mail eq '{esc}'", "User"),
        ("/groups", f"displayName eq '{esc}'", "Group"),
        ("/servicePrincipals", f"displayName eq '{esc}'", "ServicePrincipal"),
    ]
    for path, filt, ptype in queries:
        try:
            r = httpx.get(f"https://graph.microsoft.com/v1.0{path}",
                          headers=headers,
                          params={"$filter": filt, "$select": "id"}, timeout=15)
            if r.status_code == 200:
                vals = r.json().get("value") or []
                if vals:
                    return (vals[0]["id"], ptype)
        except Exception:
            continue
    return None


@lru_cache(maxsize=512)
def _resolve_display_name_to_upn(display_name: str, principal_type: str) -> Optional[str]:
    """Map a Fabric *display name* ("Ada Lovelace") to a UC principal name.

    Unity Catalog keys users by UPN/email, so a display name taken from a Fabric
    role assignment or data access role is not a usable UC principal and the
    grant fails with PRINCIPAL_DOES_NOT_EXIST. Groups and service principals are
    matched by name in UC, so they pass through unchanged.
    """
    name = (display_name or "").strip()
    if not name:
        return None
    if (principal_type or "").lower() in ("group", "serviceprincipal"):
        return name
    try:
        token = get_token("https://graph.microsoft.com/.default")
    except Exception:
        return None
    esc = name.replace("'", "''")
    try:
        r = httpx.get(
            "https://graph.microsoft.com/v1.0/users",
            headers={"Authorization": f"Bearer {token}"},
            params={"$filter": f"displayName eq '{esc}'",
                    "$select": "userPrincipalName,mail"},
            timeout=15,
        )
        if r.status_code != 200:
            return None
        for entry in (r.json().get("value") or []):
            value = str(entry.get("userPrincipalName") or entry.get("mail") or "").strip()
            if value:
                return value
    except Exception:
        return None
    return None


@lru_cache(maxsize=512)
def _resolve_fabric_principal_to_uc_principal(principal: str, principal_type: str) -> Optional[str]:
    """Translate Fabric object ids from DARs into UC principal names."""
    p = (principal or "").strip()
    if not p:
        return None
    if not _GUID.match(p):
        # Already a UPN/email that UC understands.
        if "@" in p:
            return p
        # Otherwise this is a display name, which UC cannot resolve.
        return _resolve_display_name_to_upn(p, principal_type)
    try:
        token = get_token("https://graph.microsoft.com/.default")
    except Exception:
        return None
    headers = {"Authorization": f"Bearer {token}"}
    ptype = (principal_type or "").lower()
    if ptype == "group":
        path = f"/groups/{p}"
        select = "displayName"
        value_keys = ("displayName",)
    elif ptype == "serviceprincipal":
        path = f"/servicePrincipals/{p}"
        select = "displayName,appId"
        value_keys = ("displayName", "appId")
    else:
        path = f"/users/{p}"
        select = "userPrincipalName,mail"
        value_keys = ("userPrincipalName", "mail")
    try:
        r = httpx.get(
            f"https://graph.microsoft.com/v1.0{path}",
            headers=headers,
            params={"$select": select},
            timeout=15,
        )
        if r.status_code != 200:
            return None
        payload = r.json() or {}
        for key in value_keys:
            value = str(payload.get(key) or "").strip()
            if value:
                return value
    except Exception:
        return None
    return None


# ---------- Fabric lookup ----------
def _find_mirrored_catalog_item(workspace_id: str, catalog_name: str) -> Optional[str]:
    """Return item_id of the Fabric item matching catalog_name, if unambiguous."""
    items = fabric_rest.list_items(workspace_id)
    candidates = [i for i in items if i.get("type") == "MirroredAzureDatabricksCatalog"]
    # Prefer exact displayName match
    for it in candidates:
        if (it.get("displayName") or "").lower() == catalog_name.lower():
            return it.get("id")
    return None


# ---------- Writers ----------
def _grant_fabric_workspace_role(workspace_id: str, principal_id: str,
                                 principal_type: str, role: str) -> tuple[bool, str]:
    url = f"https://api.fabric.microsoft.com/v1/workspaces/{workspace_id}/roleAssignments"
    body = {"principal": {"id": principal_id, "type": principal_type or "User"},
            "role": role}
    r = httpx.post(url, headers={"Authorization": f"Bearer {get_fabric_token()}",
                                 "Content-Type": "application/json"},
                   json=body, timeout=30)
    if r.status_code == 409 or "PrincipalAlreadyHasWorkspaceRolePermissions" in r.text:
        return True, "already granted (idempotent)"
    if r.status_code >= 300:
        return False, f"{r.status_code} {r.text[:200]}"
    return True, "granted (workspace role)"


def _dar_rule_values(role: dict, attribute_name: str) -> set[str]:
    values: set[str] = set()
    for rule in role.get("decisionRules") or []:
        attrs = list(rule.get("permission") or []) + list(rule.get("attributes") or [])
        for attr in attrs:
            if (attr.get("attributeName") or "").lower() != attribute_name.lower():
                continue
            for key in ("attributeValueIncludedIn", "values", "value"):
                raw = attr.get(key)
                if isinstance(raw, list):
                    values.update(str(v) for v in raw)
                elif raw:
                    values.add(str(raw))
    return values


def _dar_role_matches(role: dict, path: str, actions: list[str]) -> bool:
    return path in _dar_rule_values(role, "Path") and set(actions).issubset(
        _dar_rule_values(role, "Action")
    )


def _dar_members(role: dict) -> list[dict]:
    members = role.get("members") or {}
    return (
        members.get("microsoftEntraMembers")
        or members.get("entraMembers")
        or members.get("microsoftEntraUsers")
        or []
    )


@lru_cache(maxsize=1)
def _current_entra_member() -> Optional[dict]:
    try:
        token = get_token("https://graph.microsoft.com/.default")
        r = httpx.get(
            "https://graph.microsoft.com/v1.0/me",
            headers={"Authorization": f"Bearer {token}"},
            params={"$select": "id"},
            timeout=15,
        )
        r.raise_for_status()
        oid = (r.json() or {}).get("id")
        if not oid:
            return None
        return {
            "objectId": oid,
            "tenantId": _get_tenant_id_from_token(),
            "objectType": "User",
        }
    except httpx.HTTPError:
        return None


def _members_for_fine_grained_policy(current_roles: list[dict], table_path: str) -> list[dict]:
    members_by_oid: dict[str, dict] = {}
    for role in current_roles:
        if not _dar_role_matches(role, table_path, ["Read"]):
            continue
        for member in _dar_members(role):
            oid = (member.get("objectId") or "").lower()
            if not oid:
                continue
            enriched = {
                "objectId": member.get("objectId"),
                "tenantId": member.get("tenantId") or _get_tenant_id_from_token(),
                "objectType": member.get("objectType") or "User",
            }
            members_by_oid[oid] = enriched
    if not members_by_oid:
        current = _current_entra_member()
        if current and current.get("objectId"):
            members_by_oid[current["objectId"].lower()] = current
    return list(members_by_oid.values())


def _grant_onelake_dar(workspace_id: str, item_id: str, principal_oid: str,
                       principal_type: str, access_class: str,
                       securable_scope: str) -> tuple[bool, str]:
    actions = ACCESS_TO_DAR_ACTIONS.get(access_class)
    if not actions:
        return False, f"no DAR mapping for {access_class}"
    if any(a in ("Write", "Reshare") for a in actions):
        return False, ("Write/Admin not supported on mirrored catalog DAR "
                       "(read-only mirror); no permission was applied")
    # Derive path from scope. Fabric DAR for MirroredAzureDatabricksCatalog requires
    # paths under /Tables/<schema>/<table> (schema-only paths are rejected).
    path = "*"
    if securable_scope.startswith("table:"):
        parts = securable_scope.split(":", 1)[1].split(".")
        if len(parts) >= 3:
            path = f"/Tables/{parts[1]}/{parts[2]}"
    elif securable_scope.startswith("schema:"):
        # No schema-only path support — grant on all tables matching prefix is
        # not exposed by Fabric DAR, so caller should route to workspace_role.
        return False, "schema-scope not supported by OneLake DAR; route to workspace role"
    role_hash = hashlib.sha256(
        f"{access_class}|{path}|{','.join(sorted(actions))}".encode("utf-8")
    ).hexdigest()[:10]
    role_name_raw = ("hub" + access_class.lower() + path
                     .replace("/", "").replace("*", "all"))
    role_prefix = re.sub(r"[^a-zA-Z0-9]", "", role_name_raw)[:48] or "hubrole"
    role_name = f"{role_prefix}{role_hash}"[:60]

    tenant_id = _get_tenant_id_from_token()
    member = {"objectId": principal_oid,
              "tenantId": tenant_id,
              "objectType": (principal_type or "User")}

    new_role = {
        "name": role_name,
        "decisionRules": [{
            "effect": "Permit",
            "permission": [
                {"attributeName": "Path",
                 "attributeValueIncludedIn": [path]},
                {"attributeName": "Action",
                 "attributeValueIncludedIn": actions},
            ],
        }],
        "members": {"microsoftEntraMembers": [member]},
    }

    base = (f"https://api.fabric.microsoft.com/v1/workspaces/{workspace_id}"
            f"/items/{item_id}/dataAccessRoles")
    auth_h = {"Authorization": f"Bearer {get_fabric_token()}",
              "Content-Type": "application/json"}

    # GET current roles, merge
    try:
        rg = httpx.get(base, headers=auth_h, timeout=30)
        if rg.status_code in (404, 501):
            return False, "OneLake DAR API not available on this item"
        if rg.status_code >= 300:
            return False, f"GET {rg.status_code} {rg.text[:200]}"
        current = (rg.json() or {}).get("value") or []
    except Exception as e:
        return False, f"GET error: {e!s}"

    # Match-or-replace by role name, but never merge into a role with a different scope.
    found_idx = None
    for i, r in enumerate(current):
        if (r.get("name") or "").lower() == role_name.lower():
            if not _dar_role_matches(r, path, actions):
                return False, (
                    f"existing DAR role '{role_name}' has different path/action rules; "
                    "refusing to merge members"
                )
            found_idx = i
            break
    if found_idx is None:
        current.append(new_role)
    else:
        # merge member if missing
        existing = current[found_idx]
        existing_members = ((existing.get("members") or {}).get("microsoftEntraMembers") or [])
        if any((m.get("objectId") or "").lower() == principal_oid.lower()
               for m in existing_members):
            return True, "already granted (idempotent)"
        existing_members.append(member)
        existing.setdefault("members", {})["microsoftEntraMembers"] = existing_members
        current[found_idx] = existing

    rp = httpx.put(base, headers=auth_h, json={"value": current}, timeout=60)
    if rp.status_code in (404, 501):
        return False, "OneLake DAR API not available on this item"
    if rp.status_code >= 300:
        return False, f"PUT {rp.status_code} {rp.text[:200]}"
    return True, f"granted (OneLake DAR role '{role_name}' on item {item_id[:8]})"


def _upsert_das_constraint_role(
    workspace_id: str,
    item_id: str,
    role_name: str,
    table_path: str,
    translation: FabricConstraintTranslation,
) -> tuple[bool, str]:
    base = (f"https://api.fabric.microsoft.com/v1/workspaces/{workspace_id}"
            f"/items/{item_id}/dataAccessRoles")
    auth_h = {"Authorization": f"Bearer {get_fabric_token()}",
              "Content-Type": "application/json"}

    try:
        rg = httpx.get(base, headers=auth_h, timeout=30)
        if rg.status_code in (404, 501):
            return False, "OneLake DAS API not available on this item"
        if rg.status_code >= 300:
            return False, f"GET {rg.status_code} {rg.text[:200]}"
        current = (rg.json() or {}).get("value") or []
    except httpx.HTTPError as exc:
        return False, f"GET error: {exc!s}"

    members = _members_for_fine_grained_policy(current, table_path)
    if not members:
        return False, "could not resolve any Entra member for Fabric DAS test policy"

    constraints: dict[str, list[dict]] = {}
    if translation.predicate:
        constraints["rows"] = [{
            "tablePath": table_path,
            "value": translation.predicate,
            "type": "Fabric",
        }]
    if translation.column_names:
        constraints["columns"] = [{
            "tablePath": table_path,
            "columnNames": list(translation.column_names),
            "columnEffect": "Permit",
            "columnAction": ["Read"],
        }]

    new_role = {
        "name": role_name,
        "decisionRules": [{
            "effect": "Permit",
            "permission": [
                {"attributeName": "Path", "attributeValueIncludedIn": [table_path]},
                {"attributeName": "Action", "attributeValueIncludedIn": ["Read"]},
            ],
            "constraints": constraints,
        }],
        "members": {"microsoftEntraMembers": members},
    }

    found_idx = None
    for idx, role in enumerate(current):
        if (role.get("name") or "").lower() == role_name.lower():
            if not _dar_role_matches(role, table_path, ["Read"]):
                return False, (
                    f"existing DAS role '{role_name}' has different path/action rules; "
                    "refusing to overwrite"
                )
            found_idx = idx
            break

    if found_idx is None:
        current.append(new_role)
    else:
        current[found_idx] = new_role

    rp = httpx.put(base, headers=auth_h, json={"value": current}, timeout=60)
    if rp.status_code in (404, 501):
        return False, "OneLake DAS API not available on this item"
    if rp.status_code >= 300:
        return False, f"PUT {rp.status_code} {rp.text[:200]}"
    note = f"; {translation.note}" if translation.note else ""
    return True, f"granted (Fabric DAS role '{role_name}' on item {item_id[:8]}){note}"


def _row_details_table_columns(row) -> list[str]:
    details = getattr(row, "constraint_details", {}) or {}
    raw_columns = details.get("table_columns") or []
    return [str(column) for column in raw_columns if str(column or "")]


def _load_table_columns(dbx_url: str, table_name: str) -> list[str]:
    if not table_name:
        return []
    client = DatabricksUCClient(dbx_url)
    metadata = client.get_table(table_name)
    return [
        str(column.get("name") or "")
        for column in metadata.get("columns") or []
        if isinstance(column, dict) and column.get("name")
    ]


def _load_table_column_metadata(client: DatabricksUCClient, table_name: str) -> list[dict]:
    metadata = client.get_table(table_name)
    return [
        column
        for column in metadata.get("columns") or []
        if isinstance(column, dict) and column.get("name")
    ]


def _safe_uc_name(scope: str, default_catalog: str) -> tuple[str, str, str]:
    parts = table_parts_from_scope(scope, default_catalog)
    if not parts:
        raise ValueError("Fabric-to-DBX row/column policies require table scope")
    for part in parts:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", part):
            raise ValueError(f"unsupported UC identifier '{part}'")
    return parts


def _safe_uc_type(column: dict) -> str:
    raw = str(column.get("type_text") or column.get("type_name") or "").strip()
    if not raw:
        raise ValueError(f"column '{column.get('name')}' has no UC type metadata")
    if not re.fullmatch(r"[A-Za-z0-9_(),\s]+", raw):
        raise ValueError(f"unsupported UC type '{raw}'")
    return " ".join(raw.split())


def _uc_function_name(catalog: str, schema: str, prefix: str, seed: str) -> str:
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:10]
    return f"{catalog}.{schema}.{prefix}_{digest}"


def _mask_literal_for_type(uc_type: str) -> str:
    base = uc_type.strip().lower()
    if base in {"string", "varchar", "char"}:
        return "'***MASKED***'"
    return f"CAST(NULL AS {uc_type})"


def _sql_string(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _constraint_uc_member_principals(details: dict) -> list[str]:
    principals: list[str] = []
    for member in details.get("members") or []:
        if not isinstance(member, dict):
            continue
        raw_principal = (
            member.get("objectId")
            or member.get("id")
            or member.get("userPrincipalName")
            or member.get("displayName")
            or ""
        )
        principal_type = member.get("objectType") or member.get("type") or "User"
        resolved = _resolve_fabric_principal_to_uc_principal(
            str(raw_principal),
            str(principal_type),
        )
        if resolved:
            principals.append(resolved)
    return sorted(set(principals))


def _member_scoped_row_expression(predicate: str, details: dict) -> str:
    principals = _constraint_uc_member_principals(details)
    if not principals:
        return predicate
    member_list = ", ".join(_sql_string(principal) for principal in principals)
    return f"CASE WHEN current_user() IN ({member_list}) THEN ({predicate}) ELSE true END"


def _member_scoped_mask_expression(mask_literal: str, details: dict) -> str:
    principals = _constraint_uc_member_principals(details)
    if not principals:
        return mask_literal
    member_list = ", ".join(_sql_string(principal) for principal in principals)
    return f"CASE WHEN current_user() IN ({member_list}) THEN {mask_literal} ELSE value END"


def _fabric_row_filter_uc_sql(pairing: dict, row, client: DatabricksUCClient) -> list[str]:
    catalog, schema, table = _safe_uc_name(
        getattr(row, "securable_scope", "") or "",
        pairing["uc_catalog"],
    )
    table_name = f"{catalog}.{schema}.{table}"
    details = getattr(row, "constraint_details", {}) or {}
    predicate = normalize_row_predicate(str(details.get("predicate") or ""))
    if not predicate:
        raise ValueError("Fabric row constraint predicate is unavailable")

    column_metadata = _load_table_column_metadata(client, table_name)
    columns_by_name = {str(column["name"]): column for column in column_metadata}
    input_columns = referenced_columns(predicate, columns_by_name.keys())
    if not input_columns:
        raise ValueError("Fabric row predicate did not reference any table columns")

    params = ", ".join(
        f"{column} {_safe_uc_type(columns_by_name[column])}"
        for column in input_columns
    )
    function_name = _uc_function_name(
        catalog,
        schema,
        "hub_fabric_rls",
        f"{table_name}|{predicate}|{','.join(input_columns)}",
    )
    return [
        (
            f"CREATE OR REPLACE FUNCTION {function_name}({params}) RETURNS BOOLEAN "
            f"RETURN {_member_scoped_row_expression(predicate, details)}"
        ),
        (
            f"ALTER TABLE {table_name} SET ROW FILTER {function_name} "
            f"ON ({', '.join(input_columns)})"
        ),
    ]


def _fabric_column_mask_uc_sql(pairing: dict, row, client: DatabricksUCClient) -> list[str]:
    catalog, schema, table = _safe_uc_name(
        getattr(row, "securable_scope", "") or "",
        pairing["uc_catalog"],
    )
    table_name = f"{catalog}.{schema}.{table}"
    details = getattr(row, "constraint_details", {}) or {}
    if str(details.get("column_effect") or "Permit").lower() != "permit":
        raise ValueError("only Fabric Permit column constraints can map to UC masks")
    column_actions = {str(action).lower() for action in details.get("column_action") or ["Read"]}
    if "read" not in column_actions:
        raise ValueError("Fabric column constraint must include Read action")

    visible_columns = {str(column) for column in details.get("columns") or []}
    if not visible_columns:
        raise ValueError("Fabric column constraint has no permitted columns")

    column_metadata = _load_table_column_metadata(client, table_name)
    columns_by_name = {str(column["name"]): column for column in column_metadata}
    missing = sorted(visible_columns.difference(columns_by_name))
    if missing:
        raise ValueError(f"Fabric column constraint references missing UC columns: {missing}")

    hidden_columns = [
        name for name in columns_by_name
        if name not in visible_columns
    ]
    if not hidden_columns:
        raise ValueError("Fabric column constraint does not hide any UC columns")
    if len(hidden_columns) > 1:
        raise ValueError(
            "Fabric column constraint hides multiple columns; UC masks are per-column, "
            "so apply one hidden-column policy at a time"
        )

    hidden = hidden_columns[0]
    uc_type = _safe_uc_type(columns_by_name[hidden])
    function_name = _uc_function_name(
        catalog,
        schema,
        "hub_fabric_mask",
        f"{table_name}|{hidden}|{','.join(sorted(visible_columns))}",
    )
    return [
        (
            f"CREATE OR REPLACE FUNCTION {function_name}(value {uc_type}) RETURNS {uc_type} "
            f"RETURN {_member_scoped_mask_expression(_mask_literal_for_type(uc_type), details)}"
        ),
        f"ALTER TABLE {table_name} ALTER COLUMN {hidden} SET MASK {function_name}",
    ]


def _fabric_constraint_uc_sql(pairing: dict, row) -> list[str]:
    client = DatabricksUCClient(pairing["dbx_workspace_url"])
    constraint_kind = getattr(row, "constraint_kind", "") or ""
    if constraint_kind == "row_filter":
        return _fabric_row_filter_uc_sql(pairing, row, client)
    if constraint_kind == "column_mask":
        return _fabric_column_mask_uc_sql(pairing, row, client)
    raise ValueError(f"unsupported fine-grained constraint kind '{constraint_kind}'")


def _grant_uc_constraint(pairing: dict, row) -> tuple[bool, bool, str]:
    warehouse_id = (settings.DBX_WAREHOUSE_ID or "").strip()
    if not warehouse_id:
        return (
            True,
            True,
            "skipped: DBX_WAREHOUSE_ID is required for Fabric-to-DBX RLS/CLS apply",
        )
    try:
        client = DatabricksUCClient(pairing["dbx_workspace_url"])
        statements = (
            _fabric_row_filter_uc_sql(pairing, row, client)
            if getattr(row, "constraint_kind", "") == "row_filter"
            else _fabric_column_mask_uc_sql(pairing, row, client)
        )
        client.ensure_sql_warehouse_running(warehouse_id)
        for statement in statements:
            client.execute_sql(warehouse_id, statement)
    except (httpx.HTTPError, RuntimeError, TimeoutError, ValueError) as exc:
        return False, False, str(exc)
    return True, False, "granted (UC fine-grained policy)"


def _fabric_constraint_translation(pairing: dict, row) -> FabricConstraintTranslation:
    details = getattr(row, "constraint_details", {}) or {}
    constraint_kind = getattr(row, "constraint_kind", "") or ""
    scope = getattr(row, "securable_scope", "") or ""
    catalog = pairing["uc_catalog"]
    dbx_url = pairing["dbx_workspace_url"]

    if constraint_kind == "row_filter":
        function_name = str(details.get("function") or "")
        if not function_name:
            raise ValueError("UC row-filter function name is unavailable")
        client = DatabricksUCClient(dbx_url)
        function_info = client.get_function(function_name)
        input_columns = [str(column) for column in details.get("input_columns") or []]
        return build_fabric_row_constraint(
            scope,
            catalog,
            function_info,
            input_columns,
            allow_test_degraded=not settings.is_production,
        )

    if constraint_kind == "column_mask":
        table_name = str(details.get("table") or "")
        table_columns = _row_details_table_columns(row) or _load_table_columns(dbx_url, table_name)
        return build_fabric_column_constraint(
            scope,
            catalog,
            str(details.get("column") or ""),
            table_columns,
        )

    raise ValueError(f"unsupported fine-grained constraint kind '{constraint_kind}'")


def _grant_fabric_constraint(pairing: dict, row, item_id: str) -> tuple[bool, bool, str]:
    scope = getattr(row, "securable_scope", "") or ""
    constraint_kind = getattr(row, "constraint_kind", "") or ""
    try:
        translation = _fabric_constraint_translation(pairing, row)
        table_path = fabric_table_path(scope, pairing["uc_catalog"])
    except (httpx.HTTPError, ValueError) as exc:
        return True, True, f"skipped: {exc}"

    role_hash = hashlib.sha256(
        f"{constraint_kind}|{table_path}|{translation.predicate}|{','.join(translation.column_names)}".encode("utf-8")
    ).hexdigest()[:10]
    role_name = f"hubfg{constraint_kind.replace('_', '')}{role_hash}"[:60]
    ok, message = _upsert_das_constraint_role(
        pairing["fabric_workspace_id"],
        item_id,
        role_name,
        table_path,
        translation,
    )
    return ok, False, message


def _grant_uc_privileges(dbx_url: str, securable_type: str, full_name: str,
                         principal: str, privileges: list[str]) -> tuple[bool, str]:
    try:
        client = DatabricksUCClient(dbx_url)
        for target_type, target_name, target_privileges in _uc_grant_operations(
            securable_type, full_name, privileges
        ):
            if not target_privileges:
                continue
            client.update_grants(
                target_type,
                target_name,
                [{"principal": principal, "add": target_privileges}],
            )
    except httpx.HTTPStatusError as exc:
        response = exc.response
        return False, f"{response.status_code} {response.text[:200]}"
    except ValueError as exc:
        return False, str(exc)
    return True, "granted (UC)"


# ---------- Planner ----------
def _plan_fabric_layer(securable_scope: str, access_class: str) -> str:
    # Only table-scope rows go to OneLake DAR (Fabric rejects schema-only paths).
    if securable_scope.startswith("schema:"):
        return "unsupported_schema"
    if securable_scope.startswith("table:") and access_class in ACCESS_TO_DAR_ACTIONS:
        return "onelake_dar"
    return "workspace_role"


def _uc_target_from_scope(securable_scope: str, default_catalog: str) -> tuple[str, str, str]:
    """Return (securable_type, full_name, layer) for a canonical diff scope."""
    scope_type, _, raw_name = (securable_scope or "").partition(":")
    name = raw_name.strip()

    if scope_type in {"", "workspace", "catalog"}:
        catalog_name = name if scope_type == "catalog" and name else default_catalog
        return "catalog", catalog_name, "uc_catalog_grant"

    if scope_type == "schema":
        parts = [part for part in name.split(".") if part]
        full_name = name if len(parts) >= 2 else f"{default_catalog}.{name}"
        return "schema", full_name, "uc_schema_grant"

    if scope_type == "table":
        parts = [part for part in name.split(".") if part]
        full_name = name if len(parts) >= 3 else f"{default_catalog}.{name}"
        return "table", full_name, "uc_table_grant"

    return "catalog", default_catalog, "uc_catalog_grant"


def _uc_privileges_for_scope(access_class: str, securable_type: str) -> list[str]:
    scoped = UC_PRIV_BY_SCOPE.get(securable_type, {})
    if access_class in scoped:
        return scoped[access_class]
    return ACCESS_TO_UC_PRIV.get(access_class) or []


def _uc_parent_grants(securable_type: str, full_name: str) -> list[tuple[str, str, list[str]]]:
    parts = [part for part in full_name.split(".") if part]
    grants: list[tuple[str, str, list[str]]] = []
    if securable_type in {"schema", "table"} and parts:
        grants.append(("catalog", parts[0], ["USE_CATALOG"]))
    if securable_type == "table" and len(parts) >= 2:
        grants.append(("schema", ".".join(parts[:2]), ["USE_SCHEMA"]))
    return grants


def _uc_grant_operations(
    securable_type: str,
    full_name: str,
    privileges: list[str],
) -> list[tuple[str, str, list[str]]]:
    return _uc_parent_grants(securable_type, full_name) + [
        (securable_type, full_name, privileges)
    ]


def apply_rows(pairing: dict, rows: list, direction: str, dry_run: bool = True
               ) -> ApplyReport:
    report = ApplyReport(dry_run=dry_run)
    fabric_ws = pairing["fabric_workspace_id"]
    uc_catalog = pairing["uc_catalog"]

    for row in rows:
        principal = getattr(row, "principal_display", "") or ""
        ptype = getattr(row, "principal_type", "User")
        access = getattr(row, "access_class", "") or ""
        scope = getattr(row, "securable_scope", "") or ""
        constraint_kind = getattr(row, "constraint_kind", "") or ""

        if constraint_kind:
            layer = constraint_kind
            target = f"review fine-grained security policy {constraint_kind}"
            # Unresolved (abstract) ABAC policies have no static Fabric equivalent
            # because they apply dynamically via governed tags. They are review-only
            # and always skipped. Resolved ABAC targets carry
            # source="databricks_abac_resolved" and flow through the normal Fabric
            # DAS apply path below like any explicit row/column constraint.
            if (getattr(row, "constraint_details", {}) or {}).get("source") == "databricks_abac":
                report.actions.append(ApplyAction(
                    direction,
                    principal,
                    scope,
                    access,
                    target,
                    layer=layer,
                    ok=True,
                    skipped=True,
                    message=(
                        "skipped: Databricks ABAC tag-driven policy has no Fabric "
                        "OneLake equivalent; review-only, not applied"
                    ),
                ))
                continue
            if direction == "dbx_to_fabric" and constraint_kind in {"row_filter", "column_mask"}:
                try:
                    item_id = _find_mirrored_catalog_item(fabric_ws, uc_catalog)
                except Exception as exc:
                    report.actions.append(ApplyAction(
                        direction,
                        principal,
                        scope,
                        access,
                        target,
                        layer=layer,
                        ok=False,
                        message=f"failed to verify Fabric mirrored catalog item: {exc}",
                    ))
                    continue
                action = ApplyAction(
                    direction,
                    principal,
                    scope,
                    access,
                    f"apply Fabric DAS {constraint_kind}",
                    layer=layer,
                )
                if not item_id:
                    action.ok = True
                    action.skipped = True
                    action.message = (
                        "skipped: no exact Fabric mirrored catalog item match; "
                        "refusing workspace-role fallback"
                    )
                elif dry_run:
                    try:
                        translation = _fabric_constraint_translation(pairing, row)
                        action.ok = True
                        action.message = (
                            "dry-run (would apply Fabric DAS "
                            f"{constraint_kind} on {translation.table_path})"
                        )
                        if translation.note:
                            action.message += f"; {translation.note}"
                    except (httpx.HTTPError, ValueError) as exc:
                        action.ok = True
                        action.skipped = True
                        action.message = f"would skip: {exc}"
                else:
                    action.ok, action.skipped, action.message = _grant_fabric_constraint(
                        pairing,
                        row,
                        item_id,
                    )
                report.actions.append(action)
                continue

            if direction == "fabric_to_dbx" and constraint_kind in {"row_filter", "column_mask"}:
                action = ApplyAction(
                    direction,
                    principal,
                    scope,
                    access,
                    f"apply UC {constraint_kind}",
                    layer=constraint_kind,
                )
                if dry_run:
                    try:
                        statements = _fabric_constraint_uc_sql(pairing, row)
                        action.ok = True
                        action.message = (
                            "dry-run (would apply UC fine-grained policy with "
                            f"{len(statements)} SQL statements)"
                        )
                    except (httpx.HTTPError, ValueError) as exc:
                        action.ok = True
                        action.skipped = True
                        action.message = f"would skip: {exc}"
                else:
                    action.ok, action.skipped, action.message = _grant_uc_constraint(
                        pairing,
                        row,
                    )
                report.actions.append(action)
                continue

            action = ApplyAction(
                direction,
                principal,
                scope,
                access,
                f"review fine-grained security policy {constraint_kind}",
                layer=constraint_kind,
                ok=True,
                skipped=True,
                message=(
                    "skipped: this row/column policy could not be translated safely "
                    "for automatic apply"
                ),
            )
            report.actions.append(action)
            continue

        if direction == "dbx_to_fabric":
            layer = _plan_fabric_layer(scope, access)
            if layer == "onelake_dar":
                try:
                    item_id = _find_mirrored_catalog_item(fabric_ws, uc_catalog)
                except Exception as exc:
                    action = ApplyAction(
                        direction,
                        principal,
                        scope,
                        access,
                        f"verify exact mirrored catalog item for {uc_catalog}",
                        layer=layer,
                        ok=False,
                        message=f"failed to verify Fabric mirrored catalog item: {exc}",
                    )
                    report.actions.append(action)
                    continue
                if not item_id:
                    action = ApplyAction(
                        direction,
                        principal,
                        scope,
                        access,
                        f"no exact mirrored catalog item for {uc_catalog}",
                        layer=layer,
                        ok=True,
                        skipped=True,
                        message=(
                            "skipped: no exact Fabric mirrored catalog item match; "
                            "refusing workspace-role fallback"
                        ),
                    )
                    report.actions.append(action)
                    continue

            if layer == "unsupported_schema":
                action = ApplyAction(
                    direction,
                    principal,
                    scope,
                    access,
                    "unsupported schema-scope DBX to Fabric grant",
                    layer=layer,
                    ok=True,
                    skipped=True,
                    message="skipped: no safe Fabric equivalent for DBX schema-scope grants",
                )
                report.actions.append(action)
                continue

            if layer == "workspace_role":
                role = ACCESS_TO_FABRIC_ROLE.get(access)
                if access == "OBJECT_USE":
                    report.actions.append(ApplyAction(
                        direction, principal, scope, access,
                        "traverse-only privilege, no Fabric workspace role",
                        layer=layer, ok=True, skipped=True,
                        message=("skipped: USE_CATALOG/USE_SCHEMA grants no data access on "
                                 "its own; granting a Fabric role would widen access"),
                    ))
                    continue
                target = (f"GRANT {role} on workspace {fabric_ws}"
                          if role else f"no mapping for {access}")
                action = ApplyAction(direction, principal, scope, access, target,
                                     layer=layer)
                if not role:
                    action.ok = False
                    action.message = "no fabric mapping"
                elif dry_run:
                    if _is_dbx_internal_principal(principal):
                        action.ok = True
                        action.skipped = True
                        action.message = "would skip (DBX-internal principal, no Entra mapping)"
                    else:
                        action.ok = True
                        action.message = "dry-run (would apply)"
                else:
                    if _is_dbx_internal_principal(principal):
                        action.ok = True
                        action.skipped = True
                        action.message = "skipped: DBX-internal principal (no Entra mapping)"
                    else:
                        resolved = _resolve_entra(principal)
                        if not resolved:
                            action.ok = True
                            action.skipped = True
                            action.message = f"skipped: '{principal}' not found in Entra"
                            _queue_unmapped(pairing, principal, ptype,
                                            "not found in Entra during workspace-role apply")
                        else:
                            oid, resolved_type = resolved
                            ok, msg = _grant_fabric_workspace_role(
                                fabric_ws, oid, resolved_type, role)
                            action.ok, action.message = ok, msg
                report.actions.append(action)

            else:  # onelake_dar
                target = (f"GRANT OneLake DAR ({access}) on item {item_id[:8]} "
                          f"scope {scope}")
                action = ApplyAction(direction, principal, scope, access, target,
                                     layer=layer)
                if access != "DATA_READ":
                    action.ok = True
                    action.skipped = True
                    action.message = (
                        "skipped: mirrored Databricks catalog DAR is read-only; "
                        "write/admin access cannot be represented safely"
                    )
                    report.actions.append(action)
                    continue
                if dry_run:
                    if _is_dbx_internal_principal(principal):
                        action.ok = True
                        action.skipped = True
                        action.message = "would skip (DBX-internal principal, no Entra mapping)"
                    else:
                        action.ok = bool(access in ACCESS_TO_DAR_ACTIONS)
                        action.message = ("dry-run (would apply)"
                                          if action.ok else "no DAR mapping")
                else:
                    if _is_dbx_internal_principal(principal):
                        action.ok = True
                        action.skipped = True
                        action.message = "skipped: DBX-internal principal (no Entra mapping)"
                    else:
                        resolved = _resolve_entra(principal)
                        if not resolved:
                            action.ok = True
                            action.skipped = True
                            action.message = f"skipped: '{principal}' not found in Entra"
                            _queue_unmapped(pairing, principal, ptype,
                                            "not found in Entra during OneLake DAR apply")
                        else:
                            oid, resolved_type = resolved
                            ok, msg = _grant_onelake_dar(
                                fabric_ws, item_id, oid, resolved_type, access, scope)
                            action.ok, action.message = ok, msg
                report.actions.append(action)

        elif direction == "fabric_to_dbx":
            securable_type, full_name, layer = _uc_target_from_scope(scope, uc_catalog)
            privs = _uc_privileges_for_scope(access, securable_type)
            target = "; ".join(
                f"GRANT {','.join(target_privs) or '(none)'} on {target_type} {target_name}"
                for target_type, target_name, target_privs in _uc_grant_operations(
                    securable_type, full_name, privs
                )
            )
            action = ApplyAction(direction, principal, scope, access, target,
                                 layer=layer)
            if not privs:
                action.ok = False
                action.message = f"no UC mapping for {access} on {securable_type}"
            elif dry_run:
                action.ok = True
                action.message = "dry-run (would apply)"
            else:
                uc_principal = _resolve_fabric_principal_to_uc_principal(principal, ptype)
                if not uc_principal:
                    action.ok = True
                    action.skipped = True
                    action.message = (
                        f"skipped: Fabric principal '{principal}' could not be resolved "
                        "to a UC principal name"
                    )
                    report.actions.append(action)
                    continue
                ok, msg = _grant_uc_privileges(
                    pairing["dbx_workspace_url"], securable_type, full_name,
                    uc_principal, privs)
                action.ok, action.message = ok, msg
            report.actions.append(action)
    return report
