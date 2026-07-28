"""Fabric Unified Permission Hub — FastAPI application entry point."""
from __future__ import annotations
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.config import settings
from app.routers import (
    home,
    pairings as pairings_router,
    databricks as databricks_router,
    fabric as fabric_router,
    inventory as inventory_router,
    capabilities as capabilities_router,
    sources as sources_router,
    sync as sync_router,
    operations as operations_router,
    auth as auth_router,
)


BASE_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

settings.validate_runtime_security()


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    """Start background services on boot and stop them on shutdown."""
    import threading
    from app.services.azure_identity import warm_credential_cache
    from app.services import db, scheduler

    db.init_db()
    threading.Thread(target=warm_credential_cache, daemon=True).start()
    scheduler.start(settings.SCAN_INTERVAL_MINUTES)
    try:
        yield
    finally:
        scheduler.stop()


app = FastAPI(title=settings.APP_NAME, debug=settings.APP_DEBUG, lifespan=_lifespan)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
app.state.templates = templates


@app.middleware("http")
async def _auth_middleware(request: Request, call_next):
    """Populate request.state.user from the session cookie; enforce login when
    AUTH_ENABLED. No-op (anonymous admin) when auth is disabled."""
    from app.services import auth as auth_svc

    if settings.AUTH_ENABLED:
        user = auth_svc.decode_session(request.cookies.get(auth_svc.SESSION_COOKIE))
        request.state.user = user
        path = request.url.path
        if user is None and not auth_svc.is_public_path(path):
            from fastapi.responses import RedirectResponse
            return RedirectResponse(url=f"/auth/login?next={path}", status_code=303)
    else:
        # Auth disabled: treat the caller as a local admin so role checks pass.
        request.state.user = {"email": "local@dev", "name": "Local", "roles": ["viewer", "approver", "admin"]}
    return await call_next(request)


app.include_router(home.router)
app.include_router(pairings_router.router)
app.include_router(databricks_router.router)
app.include_router(fabric_router.router)
app.include_router(inventory_router.router)
app.include_router(capabilities_router.router)
app.include_router(sources_router.router)
app.include_router(sync_router.router)
app.include_router(operations_router.router)
app.include_router(operations_router.api)
app.include_router(auth_router.router)


@app.exception_handler(404)
def not_found(request: Request, exc):
    return HTMLResponse(
        templates.get_template("base.html").render(
            request=request, settings=settings,
            content="<div class='p-8'><h1 class='text-2xl font-bold'>Not found</h1></div>",
        ),
        status_code=404,
    )

