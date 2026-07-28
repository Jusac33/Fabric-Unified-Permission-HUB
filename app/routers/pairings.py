from collections import defaultdict

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.config import settings
from app.services import pairings as pairings_service
from app.services import pair_diff, pair_apply
from app.services import cache as _cache
from app.services import drift_service
from app.services import approvals
from app.services.audit_log import record_permission_apply, record_permission_apply_start
from app.services.databricks_rest import DatabricksUCClient
from app.validation import (
    require_choice,
    require_databricks_workspace_url,
    require_pairing_id,
    require_safe_path_segment,
    require_uuid,
)

router = APIRouter(prefix="/pairings")

# Scope-type labels, colors, and apply-support metadata for the grouped diff UI.
# Sources:
#   Fabric workspace roles: https://learn.microsoft.com/en-us/fabric/fundamentals/roles-workspaces
#   Fabric permission model: https://learn.microsoft.com/en-us/fabric/security/permission-model
#   OneLake DAR (Data Access Roles): https://learn.microsoft.com/en-us/fabric/onelake/security/data-access-control-model
#   UC privileges reference: https://learn.microsoft.com/en-us/azure/databricks/data-governance/unity-catalog/manage-privileges/privileges
#
# Mapping logic:
#   DBX → Fabric:
#     ACCESS_CLASS   Catalog/Workspace scope  Table scope                 Schema scope
#     DATA_READ      Viewer workspace role    OneLake DAR Read            Not supported (DAR needs table path)
#     DATA_WRITE     Contributor ws role      Not supported (read-only)    Not supported
#     DATA_ADMIN     Admin workspace role     Not supported (read-only)    Not supported
#     OBJECT_USE     Viewer workspace role    (not applicable)            Not supported
#     Table scope requires an exact mirrored catalog item match; no workspace-role fallback.
#
#   Fabric → DBX:
#     ACCESS_CLASS   UC grant is scope-aware and includes parent USE_CATALOG/USE_SCHEMA grants.
_SCOPE_META = {
    "catalog": {
        "label": "Catalog-level",
        "color": "bg-purple-100 text-purple-800",
        "dbx_to_fabric": {
            "supported": True,
            "layer": "Workspace role",
            "note": "DATA_READ→Viewer, DATA_WRITE→Contributor, DATA_ADMIN→Admin. "
                    "Workspace roles apply to all items in the workspace (docs: Fabric workspace roles).",
            "mapping": [
                ("USE_CATALOG",      "OBJECT_USE",  "Viewer"),
                ("SELECT",           "DATA_READ",   "Viewer"),
                ("CREATE_SCHEMA",    "DATA_WRITE",  "Contributor"),
                ("MODIFY",           "DATA_WRITE",  "Contributor"),
                ("ALL_PRIVILEGES",   "DATA_ADMIN",  "Admin"),
            ],
        },
        "fabric_to_dbx": {
            "supported": True,
            "layer": "UC catalog grant",
            "note": "Viewer→SELECT, Contributor→MODIFY, Admin→ALL_PRIVILEGES on the catalog "
                    "(privilege inheritance flows to all schemas/tables per UC docs).",
            "mapping": [
                ("Viewer",       "DATA_READ",   "SELECT"),
                ("Contributor",  "DATA_WRITE",  "MODIFY"),
                ("Admin",        "DATA_ADMIN",  "ALL_PRIVILEGES"),
            ],
        },
    },
    "catalog_use": {
        "label": "Catalog parent/use",
        "color": "bg-purple-50 text-purple-700",
        "dbx_to_fabric": {
            "supported": False,
            "layer": "Review only",
            "note": "UC USE_CATALOG is a navigation/parent privilege required before users can "
                    "reach schemas or tables. Fabric has no catalog-only equivalent; applying it "
                    "would become a workspace Viewer role and broaden access to the workspace.",
            "mapping": [
                ("USE_CATALOG", "OBJECT_USE", "No narrow Fabric equivalent"),
            ],
        },
        "fabric_to_dbx": {
            "supported": True,
            "layer": "UC catalog grant",
            "note": "Fabric workspace roles map to UC catalog-level grants.",
            "mapping": [],
        },
    },
    "dbx_internal": {
        "label": "DBX internal principal",
        "color": "bg-slate-100 text-slate-700",
        "dbx_to_fabric": {
            "supported": False,
            "layer": "No Entra mapping",
            "note": "Databricks built-in/account groups such as account users or workspace-local "
                    "groups do not map directly to Microsoft Entra users/groups, so the app "
                    "does not create Fabric permissions for them automatically.",
            "mapping": [
                ("account users / _workspace_*", "Any", "Skipped safely"),
            ],
        },
        "fabric_to_dbx": {
            "supported": False,
            "layer": "No mapping",
            "note": "Internal Databricks principals are DBX-side only.",
            "mapping": [],
        },
    },
    "schema": {
        "label": "Schema-level",
        "color": "bg-sky-100 text-sky-800",
        "dbx_to_fabric": {
            "supported": False,
            "layer": "Not supported",
            "note": "OneLake DAR requires paths under /Tables/<schema>/<table> — "
                    "schema-only paths are rejected by the Fabric API. "
                    "No Fabric equivalent for UC USE_SCHEMA/schema-level SELECT "
                    "(workspace roles are coarser; OneLake DAR is finer).",
            "mapping": [],
        },
        "fabric_to_dbx": {
            "supported": True,
            "layer": "UC schema grant",
            "note": "Grants USE_SCHEMA + data privileges on the schema. "
                    "SELECT at schema level inherits to all current and future tables (per UC docs).",
            "mapping": [
                ("ReadAll",  "DATA_READ",   "SELECT on schema"),
                ("Write",    "DATA_WRITE",  "MODIFY on schema"),
            ],
        },
    },
    "table": {
        "label": "Table-level",
        "color": "bg-amber-100 text-amber-800",
        "dbx_to_fabric": {
            "supported": True,
            "layer": "OneLake DAR",
            "note": "Creates a Data Access Role on the mirrored catalog item at "
                    "/Tables/<schema>/<table> only when the Fabric item name exactly "
                    "matches the paired UC catalog. Mirrored DBX catalogs support Read only; "
                    "write/admin requests are skipped instead of downgraded.",
            "mapping": [
                ("SELECT",           "DATA_READ",   "DAR Read"),
                ("MODIFY",           "DATA_WRITE",  "Not supported on mirror"),
                ("ALL_PRIVILEGES",   "DATA_ADMIN",  "Not supported on mirror"),
            ],
        },
        "fabric_to_dbx": {
            "supported": True,
            "layer": "UC table grant",
            "note": "Grants UC privileges directly on the table. "
                    "SELECT allows querying; MODIFY allows insert/update/delete (per UC docs).",
            "mapping": [
                ("DAR Read",       "DATA_READ",   "SELECT"),
                ("DAR ReadWrite",  "DATA_WRITE",  "MODIFY"),
            ],
        },
    },
    "table_readonly": {
        "label": "Table write/admin",
        "color": "bg-orange-100 text-orange-800",
        "dbx_to_fabric": {
            "supported": False,
            "layer": "Read-only mirror",
            "note": "These are UC MODIFY or ALL_PRIVILEGES table grants. The paired Fabric item "
                    "is a mirrored Databricks catalog, so OneLake DAR can safely represent Read "
                    "only; write/admin grants are skipped instead of downgraded.",
            "mapping": [
                ("MODIFY", "DATA_WRITE", "Not supported on mirrored catalog"),
                ("ALL_PRIVILEGES", "DATA_ADMIN", "Not supported on mirrored catalog"),
            ],
        },
        "fabric_to_dbx": {
            "supported": True,
            "layer": "UC table grant",
            "note": "Fabric table permissions can become UC table grants.",
            "mapping": [],
        },
    },
    "workspace": {
        "label": "Workspace-level",
        "color": "bg-slate-200 text-slate-700",
        "dbx_to_fabric": {
            "supported": True,
            "layer": "Workspace role",
            "note": "Maps to Fabric workspace roles: Viewer (read data via SQL/OneLake), "
                    "Contributor (write items/data), Member (share), Admin (manage permissions). "
                    "Admin/Member/Contributor override OneLake DAR Read (per docs).",
            "mapping": [
                ("DATA_READ",   "DATA_READ",   "Viewer"),
                ("DATA_WRITE",  "DATA_WRITE",  "Contributor"),
                ("DATA_ADMIN",  "DATA_ADMIN",  "Admin"),
            ],
        },
        "fabric_to_dbx": {
            "supported": True,
            "layer": "UC catalog grant",
            "note": "Grants UC privileges at the catalog level. "
                    "USE_CATALOG is required to interact with any object inside a catalog (per UC docs).",
            "mapping": [
                ("Viewer",       "DATA_READ",   "SELECT + USE_CATALOG"),
                ("Contributor",  "DATA_WRITE",  "MODIFY + USE_CATALOG"),
                ("Admin",        "DATA_ADMIN",  "ALL_PRIVILEGES"),
            ],
        },
    },
    "workspace_admin_review": {
        "label": "Workspace admin",
        "color": "bg-slate-300 text-slate-800",
        "dbx_to_fabric": {
            "supported": True,
            "layer": "Workspace role",
            "note": "Catalog/admin permissions can map to Fabric workspace roles.",
            "mapping": [],
        },
        "fabric_to_dbx": {
            "supported": False,
            "layer": "Review broad admin",
            "note": "Fabric workspace Admin is intentionally broad. Applying it to UC would grant "
                    "ALL_PRIVILEGES on the paired catalog, so it is not auto-selected.",
            "mapping": [
                ("Workspace Admin", "DATA_ADMIN", "UC ALL_PRIVILEGES on catalog"),
            ],
        },
    },
    "row_filter": {
        "label": "Row-level security",
        "color": "bg-rose-100 text-rose-800",
        "dbx_to_fabric": {
            "supported": True,
            "layer": "Fabric DAS rows",
            "note": "UC row filters sync to Fabric OneLake DAS row constraints. "
                    "Unsupported predicates are skipped safely instead of being broadened.",
            "mapping": [
                ("UC row filter", "DATA_READ", "Fabric DAS constraints.rows"),
            ],
        },
        "fabric_to_dbx": {
            "supported": True,
            "layer": "UC row filter",
            "note": "Fabric DAS row predicates can be represented as Unity Catalog row-filter "
                    "functions when the predicate maps cleanly to UC SQL.",
            "mapping": [
                ("Fabric DAS constraints.rows", "DATA_READ", "UC row filter function"),
            ],
        },
    },
    "column_mask": {
        "label": "Column-level security",
        "color": "bg-fuchsia-100 text-fuchsia-800",
        "dbx_to_fabric": {
            "supported": True,
            "layer": "Fabric DAS columns",
            "note": "UC column masks sync to Fabric OneLake DAS column constraints. "
                    "Fabric represents this as column visibility, so masked columns are hidden.",
            "mapping": [
                ("UC column mask", "DATA_READ", "Fabric DAS constraints.columns"),
            ],
        },
        "fabric_to_dbx": {
            "supported": True,
            "layer": "UC column mask",
            "note": "Fabric DAS column constraints can be represented as Unity Catalog column "
                    "masks when the hidden-column semantics map cleanly.",
            "mapping": [
                ("Fabric DAS constraints.columns", "DATA_READ", "UC column mask function"),
            ],
        },
    },
}


def _diff_group_key(row, direction: str) -> str:
    scope_type = (
        getattr(row, "constraint_kind", "")
        or (row.securable_scope.split(":")[0] if ":" in row.securable_scope else "other")
    )
    if direction == "dbx_to_fabric":
        if pair_apply._is_dbx_internal_principal(getattr(row, "principal_display", "")):
            return "dbx_internal"
        if scope_type == "catalog" and getattr(row, "access_class", "") == "OBJECT_USE":
            return "catalog_use"
        if scope_type == "table" and getattr(row, "access_class", "") != "DATA_READ":
            return "table_readonly"
    if (
        direction == "fabric_to_dbx"
        and scope_type == "workspace"
        and getattr(row, "access_class", "") == "DATA_ADMIN"
    ):
        return "workspace_admin_review"
    return scope_type


def _group_rows(rows, direction="dbx_to_fabric"):
    """Group DiffRows by the scope type (catalog/schema/table/workspace)."""
    groups = defaultdict(list)
    for r in rows:
        scope_type = _diff_group_key(r, direction)
        groups[scope_type].append(r)
    # Return in a stable order
    ordered = []
    for k in (
        "row_filter",
        "column_mask",
        "dbx_internal",
        "catalog_use",
        "catalog",
        "schema",
        "table_readonly",
        "table",
        "workspace_admin_review",
        "workspace",
    ):
        if k in groups:
            meta = _SCOPE_META.get(k, {})
            support = meta.get(direction, {"supported": False, "layer": "Unknown", "note": ""})
            ordered.append({
                "key": k,
                "label": meta.get("label", k.title()),
                "color": meta.get("color", "bg-slate-100 text-slate-700"),
                "rows": groups[k],
                "supported": support["supported"],
                "layer": support["layer"],
                "note": support["note"],
                "mapping": support.get("mapping", []),
            })
    for k, v in groups.items():
        if k not in (
            "row_filter",
            "column_mask",
            "dbx_internal",
            "catalog_use",
            "catalog",
            "schema",
            "table_readonly",
            "table",
            "workspace_admin_review",
            "workspace",
        ):
            ordered.append({"key": k, "label": k.title(), "color": "bg-slate-100 text-slate-700",
                            "rows": v, "supported": False, "layer": "Unknown", "note": "", "mapping": []})
    return ordered


def _split_groups(groups):
    actionable = [g for g in groups if g["supported"]]
    review = [g for g in groups if not g["supported"]]
    return actionable, review


def _count_group_rows(groups) -> int:
    return sum(len(g["rows"]) for g in groups)


# ---------------- redirects for old URLs ----------------
@router.get("", response_class=HTMLResponse)
def pairings_index_redirect():
    """Old /pairings listing URL → redirect to home."""
    return RedirectResponse(url="/", status_code=302)


@router.get("/{pairing_id}/diff", response_class=HTMLResponse)
def diff_redirect(pairing_id: str):
    """Old /pairings/{id}/diff URL → redirect to detail page."""
    pairing_id = require_pairing_id(pairing_id)
    return RedirectResponse(url=f"/pairings/{pairing_id}", status_code=302)


# ---------------- create + delete ----------------
@router.post("")
def create(
    label: str = Form(""),
    dbx_workspace_url: str = Form(...),
    uc_catalog: str = Form(...),
    fabric_workspace_id: str = Form(...),
    notes: str = Form(""),
):
    dbx_workspace_url = require_databricks_workspace_url(
        dbx_workspace_url, "Databricks workspace URL")
    uc_catalog = require_safe_path_segment(uc_catalog, "Unity Catalog catalog")
    fabric_workspace_id = require_uuid(fabric_workspace_id, "Fabric workspace ID")
    p = pairings_service.add_pairing(
        label=label, dbx_workspace_url=dbx_workspace_url,
        uc_catalog=uc_catalog, fabric_workspace_id=fabric_workspace_id,
        notes=notes,
    )
    return RedirectResponse(url=f"/pairings/{p['id']}", status_code=303)


@router.post("/{pairing_id}/delete")
def delete(pairing_id: str):
    pairing_id = require_pairing_id(pairing_id)
    if not pairings_service.delete_pairing(pairing_id):
        raise HTTPException(status_code=404, detail="Pairing not found")
    return RedirectResponse(url="/", status_code=303)


# ---------------- pairing detail (single page with tabs) ----------------
@router.get("/{pairing_id}", response_class=HTMLResponse)
def detail_view(pairing_id: str, request: Request, refresh: int = 0):
    pairing_id = require_pairing_id(pairing_id)
    pairing = pairings_service.get_pairing(pairing_id)
    if not pairing:
        return RedirectResponse(url="/", status_code=303)
    resp = request.app.state.templates.TemplateResponse(
        request, "pairings/detail.html",
        {"settings": settings, "pairing": pairing, "buckets": None,
         "error": None, "apply_report": None, "selected_direction": "dbx_to_fabric"},
    )
    resp.headers["Cache-Control"] = "no-store"
    return resp


# -- Tab: Permission Diff (fragment) --
@router.get("/{pairing_id}/tab/diff", response_class=HTMLResponse)
def tab_diff(pairing_id: str, request: Request, refresh: int = 0):
    pairing_id = require_pairing_id(pairing_id)
    pairing = pairings_service.get_pairing(pairing_id)
    if not pairing:
        return HTMLResponse("<p class='text-rose-700'>Pairing not found.</p>")
    error = None
    buckets = {"all": [], "dbx_only": [], "fabric_only": [], "in_sync": [], "errors": []}
    try:
        buckets = pair_diff.compute_diff(
            pairing["dbx_workspace_url"], pairing["uc_catalog"],
            pairing["fabric_workspace_id"],
            use_cache=(refresh == 0),
        )
    except Exception as e:
        error = str(e)
    # Surface collection-level errors (e.g. Azure auth failure) in the UI
    if buckets.get("errors"):
        error = (error + "; " if error else "") + " | ".join(buckets["errors"])
    # Record a drift snapshot whenever we have a clean diff (no collection error).
    drift = None
    if not error:
        try:
            drift = drift_service.record_snapshot(pairing_id, buckets)
        except Exception:
            drift = None
    dbx_groups = _group_rows(buckets["dbx_only"], "dbx_to_fabric")
    fabric_groups = _group_rows(buckets["fabric_only"], "fabric_to_dbx")
    dbx_actionable_groups, dbx_review_groups = _split_groups(dbx_groups)
    fabric_actionable_groups, fabric_review_groups = _split_groups(fabric_groups)
    review_groups = dbx_review_groups + fabric_review_groups
    return request.app.state.templates.TemplateResponse(
        request, "pairings/_diff_data.html",
        {"settings": settings, "pairing": pairing, "buckets": buckets,
         "error": error,
          "drift": drift,
          "dbx_groups": dbx_groups,
          "fabric_groups": fabric_groups,
          "dbx_actionable_groups": dbx_actionable_groups,
          "fabric_actionable_groups": fabric_actionable_groups,
          "review_groups": review_groups,
          "dbx_actionable_count": _count_group_rows(dbx_actionable_groups),
          "fabric_actionable_count": _count_group_rows(fabric_actionable_groups),
          "review_count": _count_group_rows(review_groups),
          "sync_groups": _group_rows(buckets["in_sync"], "in_sync")},
    )


# -- Tab: Fabric Side (fragment) --
@router.get("/{pairing_id}/tab/fabric", response_class=HTMLResponse)
def tab_fabric(pairing_id: str, request: Request, refresh: int = 0):
    from concurrent.futures import ThreadPoolExecutor
    pairing_id = require_pairing_id(pairing_id)
    pairing = pairings_service.get_pairing(pairing_id)
    if not pairing:
        return HTMLResponse("<p class='text-rose-700'>Pairing not found.</p>")

    wid = pairing["fabric_workspace_id"]
    cache_key = f"tab_fabric::{wid}"
    if not refresh:
        cached = _cache.get(cache_key)
        if cached:
            return request.app.state.templates.TemplateResponse(
                request, "pairings/_tab_fabric.html",
                {"settings": settings, **cached},
            )

    error = None
    ws = None
    items = []
    role_assignments = []
    try:
        from app.services import fabric_rest
        with ThreadPoolExecutor(max_workers=3) as ex:
            f_ws = ex.submit(fabric_rest.get_workspace, wid)
            f_items = ex.submit(fabric_rest.list_items, wid)
            f_ra = ex.submit(fabric_rest.list_role_assignments, wid)
            ws = f_ws.result()
            items = f_items.result()
            role_assignments = f_ra.result()
    except Exception as e:
        error = str(e)

    ctx = {"ws": ws, "items": items, "role_assignments": role_assignments, "error": error}
    if not error:
        _cache.set(cache_key, ctx, ttl=300.0)
    return request.app.state.templates.TemplateResponse(
        request, "pairings/_tab_fabric.html",
        {"settings": settings, **ctx},
    )


# -- Tab: Databricks Side (fragment) --
@router.get("/{pairing_id}/tab/dbx", response_class=HTMLResponse)
def tab_dbx(pairing_id: str, request: Request, refresh: int = 0):
    from concurrent.futures import ThreadPoolExecutor
    pairing_id = require_pairing_id(pairing_id)
    pairing = pairings_service.get_pairing(pairing_id)
    if not pairing:
        return HTMLResponse("<p class='text-rose-700'>Pairing not found.</p>")

    dbx_url = pairing["dbx_workspace_url"]
    catalog = pairing["uc_catalog"]
    cache_key = f"tab_dbx::{dbx_url}::{catalog}"
    if not refresh:
        cached = _cache.get(cache_key)
        if cached:
            return request.app.state.templates.TemplateResponse(
                request, "pairings/_tab_dbx.html",
                {"settings": settings, **cached},
            )

    error = None
    schemas = []
    catalog_grants = []
    try:
        c = DatabricksUCClient(dbx_url)
        with ThreadPoolExecutor(max_workers=2) as ex:
            f_schemas = ex.submit(c.list_schemas, catalog)
            f_grants = ex.submit(c.get_grants, "catalog", catalog)
            schemas = f_schemas.result()
            catalog_grants = f_grants.result()
    except Exception as e:
        error = str(e)

    ctx = {"workspace_url": dbx_url, "catalog": catalog,
           "schemas": schemas, "catalog_grants": catalog_grants, "error": error}
    if not error:
        _cache.set(cache_key, ctx, ttl=300.0)
    return request.app.state.templates.TemplateResponse(
        request, "pairings/_tab_dbx.html",
        {"settings": settings, **ctx},
    )


# ---------------- apply (POST stays on detail page) ----------------
@router.post("/{pairing_id}/apply", response_class=HTMLResponse)
def apply_view(
    pairing_id: str, request: Request,
    direction: str = Form("dbx_to_fabric"),
    dry_run: str = Form("true"),
    row: list[str] = Form(default=[]),
    approval_id: str = Form(""),
):
    pairing_id = require_pairing_id(pairing_id)
    direction = require_choice(direction, {"dbx_to_fabric", "fabric_to_dbx"}, "direction")
    dry_run = require_choice(dry_run.lower(), {"true", "false"}, "dry_run")
    pairing = pairings_service.get_pairing(pairing_id)
    if not pairing:
        return RedirectResponse(url="/", status_code=303)

    is_dry_run = dry_run == "true"
    selected = set(row or [])

    # Approval gate: when enabled, a real apply must reference an approved request.
    approval_note = None
    if not is_dry_run and settings.REQUIRE_APPROVAL:
        approved = approvals.get(approval_id) if approval_id else None
        if not (approved and approved.get("status") == "approved"
                and approved.get("pairing_id") == pairing_id):
            new_id = approvals.create_request(
                pairing_id=pairing_id,
                direction=direction,
                row_keys=sorted(selected),
                requested_by=(request.client.host if request.client else ""),
                note="auto-created from apply request",
            )
            return RedirectResponse(
                url=f"/pairings/{pairing_id}?approval_requested={new_id}",
                status_code=303,
            )
        approval_note = approval_id

    error = None
    try:
        buckets = pair_diff.compute_diff(
            pairing["dbx_workspace_url"], pairing["uc_catalog"],
            pairing["fabric_workspace_id"],
            use_cache=False,
        )
    except Exception:
        cache_key = f"diff::{pairing['dbx_workspace_url']}::{pairing['uc_catalog']}::{pairing['fabric_workspace_id']}"
        cached = _cache.get(cache_key) if is_dry_run else None
        if is_dry_run and cached:
            buckets = cached
        else:
            buckets = {"all": [], "dbx_only": [], "fabric_only": [], "in_sync": [], "errors": []}
            error = (
                "Could not verify live permissions; real apply blocked. "
                "Run `az login` and retry."
            )

    # Surface collection-level errors from compute_diff
    if buckets.get("errors"):
        error = (error + "; " if error else "") + " | ".join(buckets["errors"])

    source_bucket = (buckets["dbx_only"]
                     if direction == "dbx_to_fabric" else buckets["fabric_only"])

    def _row_key(r):
        return getattr(
            r,
            "selection_key",
            f"{r.principal_key}|{r.securable_scope}|{r.access_class}||",
        )

    if selected:
        live_by_key = {_row_key(r): r for r in source_bucket}
        rows_to_apply = [live_by_key[k] for k in selected if k in live_by_key]
    else:
        rows_to_apply = []
    selected_row_keys = [_row_key(r) for r in rows_to_apply]

    report = None
    audit_id = None
    audit_error = None
    actor = {
        "client_host": request.client.host if request.client else "",
        "user_agent": request.headers.get("user-agent", "")[:200],
    }
    if error:
        pass  # skip apply when we couldn't load permissions
    else:
        if not is_dry_run:
            try:
                audit_id = record_permission_apply_start(
                    pairing=pairing,
                    direction=direction,
                    dry_run=is_dry_run,
                    selected_count=len(rows_to_apply),
                    actor=actor,
                )
            except OSError as exc:
                audit_error = f"Audit log write failed: {exc}"
                error = "Audit log unavailable; real apply blocked before changes."
        if not error:
            report = pair_apply.apply_rows(pairing, rows_to_apply, direction,
                                           dry_run=is_dry_run)
            if not is_dry_run and approval_note:
                try:
                    approvals.mark_executed(approval_note)
                except Exception:
                    pass

    try:
        audit_id = record_permission_apply(
            pairing=pairing,
            direction=direction,
            dry_run=is_dry_run,
            selected_count=len(rows_to_apply),
            report=report,
            error=error,
            actor=actor,
            audit_id=audit_id,
        )
    except OSError as exc:
        audit_error = f"Audit log write failed: {exc}"

    if not is_dry_run:
        _cache.invalidate("diff::")

    # After a real apply, pass buckets=None so the diff tab lazy-loads
    # fresh data via HTMX (reflecting the newly-applied permissions).
    # For dry-run, show current buckets inline so user can review & apply.
    show_buckets = buckets if is_dry_run else None
    dbx_groups = _group_rows(buckets["dbx_only"], "dbx_to_fabric") if show_buckets else []
    fabric_groups = _group_rows(buckets["fabric_only"], "fabric_to_dbx") if show_buckets else []
    dbx_actionable_groups, dbx_review_groups = _split_groups(dbx_groups)
    fabric_actionable_groups, fabric_review_groups = _split_groups(fabric_groups)
    review_groups = dbx_review_groups + fabric_review_groups

    resp = request.app.state.templates.TemplateResponse(
        request, "pairings/detail.html",
        {"settings": settings, "pairing": pairing, "buckets": show_buckets,
         "error": error, "apply_report": report,
          "audit_id": audit_id,
           "audit_error": audit_error,
           "selected_direction": direction,
           "selected_row_keys": selected_row_keys,
           "dbx_groups": dbx_groups,
          "fabric_groups": fabric_groups,
          "dbx_actionable_groups": dbx_actionable_groups,
          "fabric_actionable_groups": fabric_actionable_groups,
          "review_groups": review_groups,
          "dbx_actionable_count": _count_group_rows(dbx_actionable_groups),
          "fabric_actionable_count": _count_group_rows(fabric_actionable_groups),
          "review_count": _count_group_rows(review_groups),
          "sync_groups": _group_rows(buckets["in_sync"], "in_sync") if show_buckets else []},
    )
    resp.headers["Cache-Control"] = "no-store"
    return resp
