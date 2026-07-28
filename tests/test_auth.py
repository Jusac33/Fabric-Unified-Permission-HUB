from __future__ import annotations

import time

from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.services import auth


def test_session_round_trip():
    token = auth.encode_session({"email": "a@x.com", "roles": ["viewer"]})
    data = auth.decode_session(token)
    assert data["email"] == "a@x.com"
    assert data["roles"] == ["viewer"]


def test_tampered_session_rejected():
    token = auth.encode_session({"email": "a@x.com"})
    body, _, sig = token.partition(".")
    tampered = body + "." + ("A" * len(sig))
    assert auth.decode_session(tampered) is None


def test_expired_session_rejected(monkeypatch):
    token = auth.encode_session({"email": "a@x.com"})
    real = time.time()
    monkeypatch.setattr(auth.time, "time", lambda: real + 9 * 3600)
    assert auth.decode_session(token) is None


def test_role_mapping(monkeypatch):
    monkeypatch.setattr(settings, "AUTH_ADMIN_EMAILS", "admin@x.com")
    monkeypatch.setattr(settings, "AUTH_APPROVER_EMAILS", "approver@x.com")
    assert auth.roles_for("nobody@x.com") == ["viewer"]
    assert "approver" in auth.roles_for("approver@x.com")
    assert set(auth.roles_for("admin@x.com")) == {"viewer", "approver", "admin"}


def test_public_paths():
    assert auth.is_public_path("/auth/login")
    assert auth.is_public_path("/static/app.css")
    assert not auth.is_public_path("/pairings")


def test_app_open_when_auth_disabled():
    # Default config has AUTH_ENABLED=False; pages must be reachable anonymously.
    c = TestClient(app, raise_server_exceptions=False)
    assert c.get("/").status_code == 200
    assert c.get("/operations").status_code == 200


def test_auth_enabled_redirects_anonymous(monkeypatch):
    monkeypatch.setattr(settings, "AUTH_ENABLED", True)
    c = TestClient(app, raise_server_exceptions=False)
    r = c.get("/pairings", follow_redirects=False)
    assert r.status_code == 303
    assert "/auth/login" in r.headers["location"]
    # Public paths remain reachable.
    assert c.get("/static/app.css", follow_redirects=False).status_code in (200, 404)
