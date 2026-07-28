"""Authentication routes: Entra SSO login, callback, logout, whoami."""
from __future__ import annotations

import secrets

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse

from app.config import settings
from app.services import auth

router = APIRouter(prefix="/auth")


@router.get("/login")
def login(request: Request, next: str = "/"):
    if not settings.AUTH_ENABLED:
        return RedirectResponse(url="/", status_code=303)
    state = secrets.token_urlsafe(16) + "|" + next
    url = auth.build_auth_url(state)
    resp = RedirectResponse(url=url, status_code=303)
    # Stash state in a short-lived signed cookie to validate on callback (CSRF).
    resp.set_cookie("uph_oauth_state", auth.encode_session({"state": state}),
                    httponly=True, samesite="lax", max_age=600)
    return resp


@router.get("/callback")
def callback(request: Request, code: str = "", state: str = ""):
    if not settings.AUTH_ENABLED:
        return RedirectResponse(url="/", status_code=303)
    saved = auth.decode_session(request.cookies.get("uph_oauth_state"))
    if not saved or saved.get("state") != state:
        raise HTTPException(400, "Invalid or expired OAuth state")
    if not code:
        raise HTTPException(400, "Missing authorization code")
    claims = auth.redeem_code(code)
    email = (claims.get("preferred_username") or claims.get("email") or "").lower()
    name = claims.get("name") or email
    session = {
        "email": email,
        "name": name,
        "oid": claims.get("oid"),
        "roles": auth.roles_for(email),
    }
    next_path = state.split("|", 1)[1] if "|" in state else "/"
    resp = RedirectResponse(url=next_path or "/", status_code=303)
    resp.set_cookie(auth.SESSION_COOKIE, auth.encode_session(session),
                    httponly=True, samesite="lax")
    resp.delete_cookie("uph_oauth_state")
    return resp


@router.get("/logout")
def logout():
    resp = RedirectResponse(url="/", status_code=303)
    resp.delete_cookie(auth.SESSION_COOKIE)
    return resp


@router.get("/whoami")
def whoami(request: Request):
    user = getattr(request.state, "user", None)
    return JSONResponse({"auth_enabled": settings.AUTH_ENABLED, "user": user})
