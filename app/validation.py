"""Input validation helpers for route and REST API boundaries."""
from __future__ import annotations

import re
from urllib.parse import urlparse

from fastapi import HTTPException

UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
PAIRING_ID_RE = re.compile(r"^[a-f0-9]{10}$", re.IGNORECASE)


def require_uuid(value: str, field_name: str) -> str:
    cleaned = value.strip()
    if not UUID_RE.fullmatch(cleaned):
        raise HTTPException(status_code=400, detail=f"Invalid {field_name}")
    return cleaned


def require_pairing_id(pairing_id: str) -> str:
    cleaned = pairing_id.strip()
    if not PAIRING_ID_RE.fullmatch(cleaned):
        raise HTTPException(status_code=400, detail="Invalid pairing ID")
    return cleaned


def require_safe_path_segment(value: str, field_name: str, max_length: int = 255) -> str:
    cleaned = value.strip()
    if (
        not cleaned
        or len(cleaned) > max_length
        or cleaned in {".", ".."}
        or any(ord(char) < 32 for char in cleaned)
        or any(char in cleaned for char in ("/", "\\", "?", "#"))
    ):
        raise HTTPException(status_code=400, detail=f"Invalid {field_name}")
    return cleaned


def is_https_url(value: str) -> bool:
    parsed = urlparse(value.strip())
    return parsed.scheme == "https" and bool(parsed.netloc)


def is_databricks_workspace_url(value: str) -> bool:
    return normalize_databricks_workspace_url(value) is not None


def normalize_databricks_workspace_url(value: str) -> str | None:
    parsed = urlparse(value.strip())
    host = (parsed.hostname or "").lower()
    if (
        parsed.scheme != "https"
        or not host.endswith(".azuredatabricks.net")
        or parsed.username
        or parsed.password
    ):
        return None
    try:
        port_value = parsed.port
    except ValueError:
        return None
    port = f":{port_value}" if port_value else ""
    return f"https://{host}{port}"


def require_databricks_workspace_url(value: str, field_name: str) -> str:
    normalized = normalize_databricks_workspace_url(value)
    if not normalized:
        raise HTTPException(status_code=400, detail=f"Invalid {field_name}")
    return normalized


def require_choice(value: str, allowed: set[str], field_name: str) -> str:
    cleaned = value.strip()
    if cleaned not in allowed:
        raise HTTPException(status_code=400, detail=f"Invalid {field_name}")
    return cleaned
