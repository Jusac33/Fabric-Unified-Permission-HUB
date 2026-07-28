"""Unity Catalog grant/revoke via REST API with MI token."""
from __future__ import annotations
import logging
import httpx

from src.auth.identity import IdentityProvider
from src.model.canonical_permission import CanonicalPermission

log = logging.getLogger(__name__)


class UnityCatalogWriter:
    """Auth tier: Tier 1 — MI/DefaultAzureCredential Databricks AAD token."""

    def __init__(self, identity: IdentityProvider):
        self._identity = identity

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._identity.get_databricks_token()}",
                "Content-Type": "application/json"}

    def _update(self, workspace_url: str, permission: CanonicalPermission,
                target_privilege: str, add: bool) -> dict:
        path = (f"{workspace_url.rstrip('/')}/api/2.1/unity-catalog/permissions/"
                f"{permission.securable_type}/{permission.securable_fqn}")
        change_key = "add" if add else "remove"
        body = {"changes": [{
            "principal": permission.principal_entra_id,
            change_key: [target_privilege],
        }]}
        try:
            r = httpx.patch(path, headers=self._headers(), json=body, timeout=30)
            if r.status_code >= 400:
                log.warning("UC %s failed: %s %s", change_key, r.status_code, r.text[:300])
                return {"action": "error", "status": r.status_code}
            return {"action": "granted" if add else "revoked", "status": r.status_code}
        except httpx.HTTPError as e:
            log.warning("UC %s exception: %s", change_key, e)
            return {"action": "error", "exception": str(e)}

    def grant(self, permission: CanonicalPermission, target_privilege: str,
              workspace_url: str) -> dict:
        return self._update(workspace_url, permission, target_privilege, add=True)

    def revoke(self, permission: CanonicalPermission, target_privilege: str,
               workspace_url: str) -> dict:
        return self._update(workspace_url, permission, target_privilege, add=False)
