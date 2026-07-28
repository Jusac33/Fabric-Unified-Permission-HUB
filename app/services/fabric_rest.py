"""Direct Fabric REST client using user's az identity (no policy-weaver dependency)."""
from __future__ import annotations
from typing import List, Optional
import httpx
from app.services.azure_identity import get_fabric_token

BASE = "https://api.fabric.microsoft.com/v1"

# Persistent connection pool — reuses TCP+TLS connections across requests.
_client = httpx.Client(
    base_url=BASE,
    timeout=httpx.Timeout(60, connect=10),
    limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
)


def _headers() -> dict:
    return {"Authorization": f"Bearer {get_fabric_token()}", "Content-Type": "application/json"}


def list_workspaces() -> List[dict]:
    r = _client.get("/workspaces", headers=_headers())
    r.raise_for_status()
    return r.json().get("value", [])


def get_workspace(workspace_id: str) -> Optional[dict]:
    r = _client.get(f"/workspaces/{workspace_id}", headers=_headers())
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json()


def list_items(workspace_id: str, item_type: Optional[str] = None) -> List[dict]:
    url = f"/workspaces/{workspace_id}/items"
    if item_type:
        url += f"?type={item_type}"
    r = _client.get(url, headers=_headers())
    r.raise_for_status()
    return r.json().get("value", [])


def list_role_assignments(workspace_id: str) -> List[dict]:
    r = _client.get(
        f"/workspaces/{workspace_id}/roleAssignments",
        headers=_headers(),
    )
    r.raise_for_status()
    return r.json().get("value", [])


def list_data_access_policies(workspace_id: str, mirror_id: str) -> List[dict]:
    """OneLake Security data access policies on a mirrored item."""
    r = _client.get(
        f"/workspaces/{workspace_id}/items/{mirror_id}/dataAccessRoles",
        headers=_headers(),
    )
    r.raise_for_status()
    return r.json().get("value", [])


def list_shortcuts(workspace_id: str, item_id: str, path: Optional[str] = None) -> List[dict]:
    """List OneLake shortcuts for a Fabric item such as a Lakehouse."""
    params = {"path": path} if path else None
    r = _client.get(
        f"/workspaces/{workspace_id}/items/{item_id}/shortcuts",
        headers=_headers(),
        params=params,
    )
    r.raise_for_status()
    return r.json().get("value", [])


def list_workspace_shortcuts(workspace_id: str) -> List[dict]:
    """Best-effort shortcut inventory for shortcut-capable Fabric items."""
    shortcuts: list[dict] = []
    for item in list_items(workspace_id):
        if item.get("type") != "Lakehouse":
            continue
        try:
            for shortcut in list_shortcuts(workspace_id, item.get("id", "")):
                shortcuts.append({"item": item, "shortcut": shortcut})
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code not in (404, 501):
                raise
    return shortcuts
