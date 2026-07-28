from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from app.config import settings
from app.services.inventory_service import summarize_all

router = APIRouter(prefix="/inventory")


@router.get("", response_class=HTMLResponse)
def inventory_view(request: Request):
    return request.app.state.templates.TemplateResponse(
        request, "inventory.html", {"settings": settings},
    )


@router.get("/_data", response_class=HTMLResponse)
def inventory_data(request: Request):
    rows = summarize_all()
    return request.app.state.templates.TemplateResponse(
        request, "_inventory_data.html",
        {"settings": settings, "rows": rows},
    )
