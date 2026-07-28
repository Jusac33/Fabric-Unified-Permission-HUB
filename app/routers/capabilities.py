from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.config import settings
from app.services import shortcut_sync_preview
from app.services.source_capabilities import (
    list_source_capabilities,
    list_translation_capabilities,
)
from app.validation import require_uuid

router = APIRouter(prefix="/capabilities")


@router.get("", response_class=HTMLResponse)
def capabilities(request: Request, workspace_id: str = "", refresh: int = 0):
    shortcuts = []
    shortcut_error = ""
    workspace_id = workspace_id.strip()
    if workspace_id:
        try:
            workspace_id = require_uuid(workspace_id, "Fabric workspace ID")
            shortcuts = shortcut_sync_preview.preview_workspace_shortcuts(workspace_id)
        except Exception as exc:
            shortcut_error = str(exc)
    return request.app.state.templates.TemplateResponse(
        request,
        "capabilities.html",
        {
            "settings": settings,
            "sources": list_source_capabilities(),
            "translations": list_translation_capabilities(),
            "workspace_id": workspace_id,
            "shortcuts": shortcuts,
            "shortcut_error": shortcut_error,
        },
    )
