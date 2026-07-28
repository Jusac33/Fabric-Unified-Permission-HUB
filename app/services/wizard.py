"""Ephemeral pairing wizard state — stores the current Fabric + DBX selection
on disk so it survives page navigation. Single-user/local tool only."""
from __future__ import annotations
import json
from pathlib import Path
from typing import Optional

from app.config import settings


def _path() -> Path:
    return settings.configs_path / ".wizard.json"


def get() -> dict:
    p = _path()
    if not p.is_file():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save(state: dict) -> None:
    _path().write_text(json.dumps(state, indent=2), encoding="utf-8")


def select_fabric(workspace_id: str, display_name: str = "") -> dict:
    s = get()
    s["fabric_workspace_id"] = workspace_id.strip()
    s["fabric_workspace_name"] = display_name.strip()
    _save(s)
    return s


def select_databricks(workspace_url: str, uc_catalog: str) -> dict:
    s = get()
    s["dbx_workspace_url"] = workspace_url.rstrip("/")
    s["uc_catalog"] = uc_catalog.strip()
    _save(s)
    return s


def clear() -> None:
    p = _path()
    if p.is_file():
        p.unlink()


def is_complete(s: Optional[dict] = None) -> bool:
    s = s if s is not None else get()
    return bool(s.get("fabric_workspace_id") and s.get("dbx_workspace_url")
                and s.get("uc_catalog"))
