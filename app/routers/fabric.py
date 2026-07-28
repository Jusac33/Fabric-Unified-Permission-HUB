from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from app.config import settings
from app.services import fabric_rest
from app.services.azure_identity import whoami
from app.validation import require_uuid

router = APIRouter(prefix="/fabric")


@router.get("", response_class=HTMLResponse)
def fabric_home(request: Request):
    return request.app.state.templates.TemplateResponse(
        request, "fabric/home.html", {"settings": settings},
    )


@router.get("/_data", response_class=HTMLResponse)
def fabric_home_data(request: Request):
    error = None
    workspaces = []
    me = None
    try:
        me = whoami()
        workspaces = fabric_rest.list_workspaces()
    except Exception as e:
        error = str(e)
    return request.app.state.templates.TemplateResponse(
        request, "fabric/_home_data.html",
        {"settings": settings, "workspaces": workspaces, "me": me, "error": error},
    )


@router.get("/workspaces/{workspace_id}", response_class=HTMLResponse)
def workspace_detail(workspace_id: str, request: Request):
    workspace_id = require_uuid(workspace_id, "workspace ID")
    return request.app.state.templates.TemplateResponse(
        request, "fabric/workspace.html",
        {"settings": settings, "workspace_id": workspace_id},
    )


@router.get("/workspaces/{workspace_id}/_data", response_class=HTMLResponse)
def workspace_detail_data(workspace_id: str, request: Request):
    workspace_id = require_uuid(workspace_id, "workspace ID")
    error = None
    ws = None
    items = []
    role_assignments = []
    try:
        ws = fabric_rest.get_workspace(workspace_id)
        items = fabric_rest.list_items(workspace_id)
        role_assignments = fabric_rest.list_role_assignments(workspace_id)
    except Exception as e:
        error = str(e)
    if not ws and not error:
        return HTMLResponse("<p class='text-rose-700'>Workspace not found.</p>")
    return request.app.state.templates.TemplateResponse(
        request, "fabric/_workspace_data.html",
        {"settings": settings, "ws": ws, "items": items,
         "role_assignments": role_assignments, "error": error,
         "workspace_id": workspace_id},
    )


@router.get("/workspaces/{workspace_id}/items/{item_id}/policies", response_class=HTMLResponse)
def item_policies(workspace_id: str, item_id: str, request: Request):
    workspace_id = require_uuid(workspace_id, "workspace ID")
    item_id = require_uuid(item_id, "item ID")
    return request.app.state.templates.TemplateResponse(
        request, "fabric/policies.html",
        {"settings": settings, "workspace_id": workspace_id, "item_id": item_id},
    )


@router.get("/workspaces/{workspace_id}/items/{item_id}/policies/_data", response_class=HTMLResponse)
def item_policies_data(workspace_id: str, item_id: str, request: Request):
    workspace_id = require_uuid(workspace_id, "workspace ID")
    item_id = require_uuid(item_id, "item ID")
    error = None
    policies = []
    try:
        policies = fabric_rest.list_data_access_policies(workspace_id, item_id)
    except Exception as e:
        error = str(e)
    return request.app.state.templates.TemplateResponse(
        request, "fabric/_policies_data.html",
        {"settings": settings, "policies": policies, "error": error,
         "workspace_id": workspace_id, "item_id": item_id},
    )
