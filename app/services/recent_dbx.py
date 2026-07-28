"""Remember recently used Databricks workspace URLs."""
from __future__ import annotations
import json
from pathlib import Path
from typing import List

from app.config import settings

MAX_RECENT = 10


def _path() -> Path:
    return settings.configs_path / ".recent_dbx.json"


def list_recent() -> List[str]:
    p = _path()
    if not p.is_file():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return [u for u in data if isinstance(u, str)]
    except Exception:
        return []


def add_recent(url: str) -> None:
    url = (url or "").rstrip("/")
    if not url.startswith(("http://", "https://")):
        return
    existing = [u for u in list_recent() if u != url]
    existing.insert(0, url)
    _path().write_text(json.dumps(existing[:MAX_RECENT], indent=2),
                       encoding="utf-8")


def most_recent() -> str:
    recent = list_recent()
    return recent[0] if recent else ""
