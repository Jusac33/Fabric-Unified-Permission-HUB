from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from app.config import settings
from app.connectors import get_connector, SourceType
from app.services.inventory_service import discover_configs
from app.validation import require_choice, require_safe_path_segment

router = APIRouter(prefix="/sources")


@router.get("", response_class=HTMLResponse)
def list_sources(request: Request):
    configs = discover_configs()
    return request.app.state.templates.TemplateResponse(
        request,
        "sources/list.html",
        {"settings": settings, "configs": configs},
    )


@router.get("/{source_type}/{name}", response_class=HTMLResponse)
def source_detail(source_type: str, name: str, request: Request):
    source_type = require_choice(source_type, {item.value for item in SourceType}, "source type")
    name = require_safe_path_segment(name, "config name", max_length=128)
    cfg = next((c for c in discover_configs() if c["name"] == name), None)
    if not cfg:
        raise HTTPException(404, "Config not found")
    return request.app.state.templates.TemplateResponse(
        request, "sources/detail.html",
        {"settings": settings, "cfg": cfg, "source_type": source_type},
    )


@router.get("/{source_type}/{name}/_data", response_class=HTMLResponse)
def source_detail_data(source_type: str, name: str, request: Request):
    source_type = require_choice(source_type, {item.value for item in SourceType}, "source type")
    name = require_safe_path_segment(name, "config name", max_length=128)
    cfg = next((c for c in discover_configs() if c["name"] == name), None)
    if not cfg:
        return HTMLResponse("<p class='text-rose-700'>Config not found.</p>")
    error = None
    principals = objects = grants = []
    test_result = None
    try:
        connector = get_connector(source_type, cfg["path"])
        test_result = connector.test_connection()
        principals = connector.list_principals()
        objects = connector.list_objects()
        grants = connector.list_grants()
    except Exception as e:
        error = str(e)
    return request.app.state.templates.TemplateResponse(
        request, "sources/_detail_data.html",
        {
            "settings": settings,
            "cfg": cfg, "source_type": source_type,
            "principals": principals, "objects": objects, "grants": grants,
            "test_result": test_result, "error": error,
        },
    )
