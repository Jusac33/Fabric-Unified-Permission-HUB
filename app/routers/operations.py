"""Operations console: queryable audit trail + identity reconciliation queue,
plus a small read-only JSON API and CSV export for compliance reviews.
"""
from __future__ import annotations

import csv
import io

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse

from app.config import settings
from app.services import identity_queue, drift_service
from app.services import approvals
from app.services import rollback_service
from app.services import pairings as pairings_service
from app.services.audit_log import list_audit_events
from app.validation import require_pairing_id, require_safe_path_segment

router = APIRouter(prefix="/operations")


@router.get("", response_class=HTMLResponse)
def operations_home(request: Request):
    return request.app.state.templates.TemplateResponse(
        request,
        "operations/home.html",
        {
            "settings": settings,
            "audit": list_audit_events(limit=50),
            "identity": identity_queue.list_items(status="unresolved"),
            "identity_counts": identity_queue.counts(),
            "approvals": approvals.list_requests(),
            "require_approval": settings.REQUIRE_APPROVAL,
        },
    )


@router.post("/approvals/{approval_id}/approve")
def approve_request(approval_id: str, request: Request):
    approvals.decide(approval_id, approved=True,
                     decided_by=(request.client.host if request.client else ""))
    return RedirectResponse(url="/operations", status_code=303)


@router.post("/approvals/{approval_id}/reject")
def reject_request(approval_id: str, request: Request):
    approvals.decide(approval_id, approved=False,
                     decided_by=(request.client.host if request.client else ""))
    return RedirectResponse(url="/operations", status_code=303)


@router.post("/identity/{item_id}/resolve")
def resolve_identity(item_id: int, oid: str = Form(...)):
    identity_queue.resolve(item_id, oid.strip())
    return RedirectResponse(url="/operations", status_code=303)


@router.post("/identity/{item_id}/ignore")
def ignore_identity(item_id: int):
    identity_queue.ignore(item_id)
    return RedirectResponse(url="/operations", status_code=303)


# ---- read-only JSON API ----
api = APIRouter(prefix="/api")


@api.get("/pairings")
def api_pairings():
    return JSONResponse(pairings_service.list_pairings())


@api.get("/audit")
def api_audit(limit: int = 100, pairing_id: str = ""):
    return JSONResponse(list_audit_events(limit=limit, pairing_id=pairing_id or None))


@api.get("/pairings/{pairing_id}/drift")
def api_drift(pairing_id: str):
    pairing_id = require_pairing_id(pairing_id)
    return JSONResponse({
        "latest": drift_service.latest_snapshot(pairing_id),
        "history": drift_service.snapshot_history(pairing_id, limit=30),
    })


@api.get("/audit/{audit_id}/rollback-plan")
def api_rollback_plan(audit_id: str):
    audit_id = require_safe_path_segment(audit_id, "audit id", max_length=64)
    plan = rollback_service.build_plan(audit_id)
    if plan is None:
        return JSONResponse({"error": "audit event not found"}, status_code=404)
    return JSONResponse(plan)


@api.get("/audit.csv")
def api_audit_csv(limit: int = 1000):
    rows = list_audit_events(limit=limit)
    buf = io.StringIO()
    fields = ["timestamp", "event", "pairing_id", "direction", "dry_run",
              "status", "selected_count", "ok_count", "skipped_count",
              "failed_count", "error"]
    writer = csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    for r in rows:
        writer.writerow(r)
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=audit.csv"},
    )
