"""Entra SSO authentication with a stdlib HMAC-signed session cookie.

Disabled by default (``AUTH_ENABLED=false``) so local development and tests are
unaffected. When enabled, unauthenticated users are redirected to Entra; on
return their email is mapped to a role (viewer / approver / admin) from the
configured email allowlists.

The session cookie stores only non-secret identity claims (email, name, roles)
and is integrity-protected with HMAC-SHA256 keyed by ``SECRET_KEY``. It is not
encrypted — do not place secrets in it.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Optional

from app.config import settings

SESSION_COOKIE = "uph_session"
_SESSION_TTL = 8 * 3600  # 8 hours

# Paths reachable without authentication.
PUBLIC_PREFIXES = ("/auth/", "/static/", "/healthz")


def _key() -> bytes:
    return (settings.SECRET_KEY or "change-me").encode("utf-8")


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64d(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def encode_session(data: dict) -> str:
    payload = dict(data)
    payload["exp"] = int(time.time()) + _SESSION_TTL
    body = _b64e(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    sig = _b64e(hmac.new(_key(), body.encode("ascii"), hashlib.sha256).digest())
    return f"{body}.{sig}"


def decode_session(token: Optional[str]) -> Optional[dict]:
    if not token or "." not in token:
        return None
    body, _, sig = token.partition(".")
    expected = _b64e(hmac.new(_key(), body.encode("ascii"), hashlib.sha256).digest())
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        data = json.loads(_b64d(body))
    except Exception:
        return None
    if int(data.get("exp", 0)) < int(time.time()):
        return None
    return data


def roles_for(email: str) -> list[str]:
    """Resolve roles from configured allowlists. Everyone authenticated is a viewer."""
    e = (email or "").strip().lower()
    roles = ["viewer"]
    if e in settings.approver_emails or e in settings.admin_emails:
        roles.append("approver")
    if e in settings.admin_emails:
        roles.append("admin")
    return roles


def is_public_path(path: str) -> bool:
    return any(path.startswith(p) for p in PUBLIC_PREFIXES) or path == "/favicon.ico"


# --- MSAL confidential client ---
def _msal_app():
    import msal

    if not (settings.AUTH_CLIENT_ID and settings.AUTH_CLIENT_SECRET and settings.AUTH_TENANT_ID):
        raise RuntimeError("AUTH_CLIENT_ID/SECRET/TENANT_ID must be set when AUTH_ENABLED=true")
    authority = f"https://login.microsoftonline.com/{settings.AUTH_TENANT_ID}"
    return msal.ConfidentialClientApplication(
        client_id=settings.AUTH_CLIENT_ID,
        client_credential=settings.AUTH_CLIENT_SECRET,
        authority=authority,
    )


def build_auth_url(state: str) -> str:
    return _msal_app().get_authorization_request_url(
        scopes=["User.Read"],
        state=state,
        redirect_uri=settings.AUTH_REDIRECT_URI,
    )


def redeem_code(code: str) -> dict:
    """Exchange an auth code for tokens and return the id-token claims."""
    result = _msal_app().acquire_token_by_authorization_code(
        code,
        scopes=["User.Read"],
        redirect_uri=settings.AUTH_REDIRECT_URI,
    )
    if "id_token_claims" not in result:
        raise RuntimeError(result.get("error_description") or "token exchange failed")
    return result["id_token_claims"]
