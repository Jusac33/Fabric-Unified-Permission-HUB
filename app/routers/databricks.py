from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from app.config import settings
from app.services.databricks_rest import DatabricksUCClient
from app.services import pairings as pairings_service
from app.services import recent_dbx
from app.validation import (
    is_databricks_workspace_url,
    normalize_databricks_workspace_url,
    require_safe_path_segment,
)

router = APIRouter(prefix="/databricks")


def _default_ws() -> str:
    return (pairings_service.resolve_dbx_url()
            or recent_dbx.most_recent())


def _resolve_ws(workspace_url: str, pairing_id: str | None) -> str:
    if workspace_url:
        return workspace_url
    return pairings_service.resolve_dbx_url(pairing_id) or recent_dbx.most_recent()


def _valid_url(url: str) -> bool:
    return is_databricks_workspace_url(url)


@router.get("", response_class=HTMLResponse)
def dbx_home(request: Request,
             workspace_url: str = Query(""),
             pairing: str | None = Query(None)):
    workspace_url = _resolve_ws(workspace_url, pairing)
    pairings = pairings_service.list_pairings()
    recent = recent_dbx.list_recent()
    return request.app.state.templates.TemplateResponse(
        request, "databricks/home.html",
        {"settings": settings, "workspace_url": workspace_url,
         "pairings": pairings, "selected_pairing": pairing or "",
         "recent": recent},
    )


@router.get("/_data", response_class=HTMLResponse)
def dbx_home_data(request: Request,
                  workspace_url: str = Query(""),
                  pairing: str | None = Query(None)):
    workspace_url = _resolve_ws(workspace_url, pairing)
    error = None
    catalogs = users = groups = sps = []
    pairings = pairings_service.list_pairings()
    recent = recent_dbx.list_recent()

    if not _valid_url(workspace_url):
        error = ("No Databricks workspace selected. Paste a URL in the box above, "
                 "pick a recent workspace, or add a pairing at /pairings.")
    else:
        try:
            workspace_url = normalize_databricks_workspace_url(workspace_url) or workspace_url
            c = DatabricksUCClient(workspace_url)
            catalogs = c.list_catalogs()
            recent_dbx.add_recent(workspace_url)
            recent = recent_dbx.list_recent()
            try: users = c.list_users()
            except Exception: users = []
            try: groups = c.list_groups()
            except Exception: groups = []
            try: sps = c.list_service_principals()
            except Exception: sps = []
        except Exception as e:
            error = str(e)

    return request.app.state.templates.TemplateResponse(
        request, "databricks/_home_data.html",
        {"settings": settings, "workspace_url": workspace_url,
         "catalogs": catalogs, "users": users, "groups": groups, "sps": sps,
         "pairings": pairings, "selected_pairing": pairing or "",
         "recent": recent, "error": error},
    )


@router.get("/catalog/{catalog}", response_class=HTMLResponse)
def dbx_catalog(catalog: str, request: Request,
                workspace_url: str = Query(default_factory=_default_ws)):
    catalog = require_safe_path_segment(catalog, "catalog")
    return request.app.state.templates.TemplateResponse(
        request, "databricks/catalog.html",
        {"settings": settings, "workspace_url": workspace_url, "catalog": catalog},
    )


@router.get("/catalog/{catalog}/_data", response_class=HTMLResponse)
def dbx_catalog_data(catalog: str, request: Request,
                     workspace_url: str = Query(default_factory=_default_ws)):
    catalog = require_safe_path_segment(catalog, "catalog")
    error = None
    schemas = []
    catalog_grants = []
    if not _valid_url(workspace_url):
        error = "Select a valid HTTPS Databricks workspace URL before browsing catalogs."
    else:
        try:
            c = DatabricksUCClient(workspace_url)
            schemas = c.list_schemas(catalog)
            catalog_grants = c.get_grants("catalog", catalog)
        except Exception as e:
            error = str(e)
    return request.app.state.templates.TemplateResponse(
        request, "databricks/_catalog_data.html",
        {"settings": settings, "workspace_url": workspace_url,
         "catalog": catalog, "schemas": schemas,
         "catalog_grants": catalog_grants, "error": error},
    )


@router.get("/catalog/{catalog}/schema/{schema}", response_class=HTMLResponse)
def dbx_schema(catalog: str, schema: str, request: Request,
               workspace_url: str = Query(default_factory=_default_ws)):
    catalog = require_safe_path_segment(catalog, "catalog")
    schema = require_safe_path_segment(schema, "schema")
    return request.app.state.templates.TemplateResponse(
        request, "databricks/schema.html",
        {"settings": settings, "workspace_url": workspace_url,
         "catalog": catalog, "schema": schema},
    )


@router.get("/catalog/{catalog}/schema/{schema}/_data", response_class=HTMLResponse)
def dbx_schema_data(catalog: str, schema: str, request: Request,
                    workspace_url: str = Query(default_factory=_default_ws)):
    catalog = require_safe_path_segment(catalog, "catalog")
    schema = require_safe_path_segment(schema, "schema")
    error = None
    tables = []
    schema_grants = []
    if not _valid_url(workspace_url):
        error = "Select a valid HTTPS Databricks workspace URL before browsing schemas."
    else:
        try:
            c = DatabricksUCClient(workspace_url)
            tables = c.list_tables(catalog, schema)
            schema_grants = c.get_grants("schema", f"{catalog}.{schema}")
        except Exception as e:
            error = str(e)
    return request.app.state.templates.TemplateResponse(
        request, "databricks/_schema_data.html",
        {"settings": settings, "workspace_url": workspace_url,
         "catalog": catalog, "schema": schema, "tables": tables,
         "schema_grants": schema_grants, "error": error},
    )


@router.get("/catalog/{catalog}/schema/{schema}/table/{table}", response_class=HTMLResponse)
def dbx_table(catalog: str, schema: str, table: str, request: Request,
              workspace_url: str = Query(default_factory=_default_ws)):
    catalog = require_safe_path_segment(catalog, "catalog")
    schema = require_safe_path_segment(schema, "schema")
    table = require_safe_path_segment(table, "table")
    return request.app.state.templates.TemplateResponse(
        request, "databricks/table.html",
        {"settings": settings, "workspace_url": workspace_url,
         "catalog": catalog, "schema": schema, "table": table},
    )


@router.get("/catalog/{catalog}/schema/{schema}/table/{table}/_data", response_class=HTMLResponse)
def dbx_table_data(catalog: str, schema: str, table: str, request: Request,
                   workspace_url: str = Query(default_factory=_default_ws)):
    catalog = require_safe_path_segment(catalog, "catalog")
    schema = require_safe_path_segment(schema, "schema")
    table = require_safe_path_segment(table, "table")
    error = None
    grants = []
    if not _valid_url(workspace_url):
        error = "Select a valid HTTPS Databricks workspace URL before browsing tables."
    else:
        try:
            c = DatabricksUCClient(workspace_url)
            grants = c.get_grants("table", f"{catalog}.{schema}.{table}")
        except Exception as e:
            error = str(e)
    return request.app.state.templates.TemplateResponse(
        request, "databricks/_table_data.html",
        {"settings": settings, "workspace_url": workspace_url,
         "catalog": catalog, "schema": schema, "table": table,
         "grants": grants, "error": error},
    )
