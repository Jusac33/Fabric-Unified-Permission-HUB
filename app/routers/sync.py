from fastapi import APIRouter, Form, Request, HTTPException
from fastapi.responses import HTMLResponse
from app.connectors import SourceType
from app.config import settings
from app.services.sync_service import SyncService
from app.services.inventory_service import discover_configs
from app.validation import require_choice, require_safe_path_segment

router = APIRouter(prefix="/sync")


@router.get("", response_class=HTMLResponse)
def sync_home(request: Request):
    return request.app.state.templates.TemplateResponse(
        request,
        "sync/run.html",
        {"settings": settings,
         "configs": discover_configs(), "jobs": SyncService.list_jobs()},
    )


@router.post("/run", response_class=HTMLResponse)
def run_sync(
    request: Request,
    config_name: str = Form(...),
    dry_run: bool = Form(False),
    source_type: str = Form(""),
):
    config_name = require_safe_path_segment(config_name, "config name", max_length=128)
    cfg = next((c for c in discover_configs() if c["name"] == config_name), None)
    if not cfg:
        raise HTTPException(404, "Config not found")
    if cfg.get("is_template"):
        raise HTTPException(400, "This config is a template; fill in real values before syncing.")
    # Source type is derived from the selected config (inferred from its filename
    # prefix). An optional posted value is honored only when it agrees with the
    # config, so the UI no longer needs a separate source-type selector.
    resolved_type = cfg["type"]
    if resolved_type == "unknown":
        if not source_type:
            raise HTTPException(400, "Cannot infer source type; rename the config with a databricks_/snowflake_/dataverse_ prefix.")
        resolved_type = source_type
    resolved_type = require_choice(resolved_type, {item.value for item in SourceType}, "source type")
    job = SyncService.run(resolved_type, cfg["path"], dry_run=dry_run)
    return request.app.state.templates.TemplateResponse(
        request,
        "sync/_job_card.html",
        {"settings": settings, "job": job},
    )


@router.get("/job/{job_id}", response_class=HTMLResponse)
def job_detail(job_id: str, request: Request):
    job_id = require_safe_path_segment(job_id, "job ID", max_length=12)
    job = SyncService.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    return request.app.state.templates.TemplateResponse(
        request,
        "sync/job_detail.html",
        {"settings": settings, "job": job},
    )
