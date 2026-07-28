"""Fabric discovery — Layer 1 RBAC + Layer 3 OneLake DAR (feature-flagged)."""
from __future__ import annotations
import logging
from typing import Iterable, List, Optional
from uuid import uuid4

import httpx

from src.auth.identity import IdentityProvider
from src.model.canonical_permission import CanonicalPermission
from src.translation.privilege_normalizer import PrivilegeNormalizer

log = logging.getLogger(__name__)


class FabricDiscovery:
    """Auth tier: Tier 1 (MI/DefaultAzureCredential → Power BI scope)."""

    def __init__(self, config: dict, identity: IdentityProvider,
                 normalizer: PrivilegeNormalizer, run_id: Optional[str] = None):
        self._cfg = config.get("discovery", {}).get("fabric", {})
        self._api_base = self._cfg.get("api_base", "").rstrip("/")
        self._identity = identity
        self._normalizer = normalizer
        self._run_id = run_id or uuid4().hex
        self._tag_key = self._cfg.get("workspace_filter_tag_key", "") or ""
        self._tag_value = self._cfg.get("workspace_filter_tag_value", "") or ""
        self._onelake_enabled = bool(self._cfg.get("onelake_data_access_roles_enabled", False))

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._identity.get_fabric_token()}",
                "Content-Type": "application/json"}

    def _paged(self, url: str) -> Iterable[dict]:
        next_url = url
        while next_url:
            r = httpx.get(next_url, headers=self._headers(), timeout=60)
            r.raise_for_status()
            body = r.json()
            for v in body.get("value", []) or []:
                yield v
            cont = body.get("continuationToken")
            cont_uri = body.get("continuationUri")
            if cont and cont_uri:
                next_url = cont_uri
            else:
                next_url = ""

    # --- workspaces ---
    def list_workspaces(self) -> List[dict]:
        ws = list(self._paged(f"{self._api_base}/workspaces"))
        if self._tag_key:
            ws = [w for w in ws if (w.get("tags") or {}).get(self._tag_key) == self._tag_value]
        return ws

    def list_role_assignments(self, workspace_id: str) -> List[dict]:
        try:
            return list(self._paged(f"{self._api_base}/workspaces/{workspace_id}/roleAssignments"))
        except httpx.HTTPError as e:
            log.warning("roleAssignments failed for %s: %s", workspace_id, e)
            return []

    def list_items(self, workspace_id: str, item_type: Optional[str] = None) -> List[dict]:
        url = f"{self._api_base}/workspaces/{workspace_id}/items"
        if item_type:
            url += f"?type={item_type}"
        try:
            return list(self._paged(url))
        except httpx.HTTPError as e:
            log.warning("items failed for %s: %s", workspace_id, e)
            return []

    def list_data_access_roles(self, workspace_id: str, item_id: str) -> List[dict]:
        if not self._onelake_enabled:
            return []
        try:
            r = httpx.get(
                f"{self._api_base}/workspaces/{workspace_id}/items/{item_id}/dataAccessRoles",
                headers=self._headers(), timeout=30,
            )
            if r.status_code in (404, 501):
                return []
            r.raise_for_status()
            return r.json().get("value", []) or []
        except httpx.HTTPError as e:
            log.warning("DAR preview failed for %s/%s: %s", workspace_id, item_id, e)
            return []

    def discover_all(self) -> List[CanonicalPermission]:
        out: List[CanonicalPermission] = []
        workspaces = self.list_workspaces()
        log.info("Fabric: discovered %d workspace(s)", len(workspaces))

        for ws in workspaces:
            ws_id = ws.get("id") or ""
            # Layer 1: workspace RBAC
            for ra in self.list_role_assignments(ws_id):
                principal = ra.get("principal") or {}
                role_name = ra.get("role", "") or ra.get("roleName", "")
                access = self._normalizer.normalize(role_name, "fabric")
                if not access:
                    continue
                out.append(CanonicalPermission(
                    principal_entra_id=principal.get("id", ""),
                    principal_type=principal.get("type", "User"),
                    principal_display_name=principal.get("displayName", ""),
                    access_class=access,
                    securable_type="workspace",
                    securable_fqn=ws.get("displayName", ws_id),
                    workspace_id=ws_id,
                    source_platform="fabric",
                    source_raw_privilege=role_name,
                    source_raw_object=ra,
                    sync_run_id=self._run_id,
                ))

            # Layer 3: OneLake DAR (preview, flag-gated)
            if not self._onelake_enabled:
                continue
            for item in self.list_items(ws_id, "Lakehouse"):
                for dar in self.list_data_access_roles(ws_id, item.get("id", "")):
                    for member in (dar.get("members") or {}).get("entraMembers", []) or []:
                        for rule in dar.get("decisionRules") or []:
                            for perm in rule.get("permission") or []:
                                actions = perm.get("attributeValueIncludedIn") or []
                                for action in actions:
                                    access = self._normalizer.normalize(action, "fabric")
                                    if not access:
                                        continue
                                    out.append(CanonicalPermission(
                                        principal_entra_id=member.get("objectId", ""),
                                        principal_type=member.get("objectType", "User"),
                                        principal_display_name=member.get("displayName", ""),
                                        access_class=access,
                                        securable_type="lakehouse",
                                        securable_fqn=item.get("displayName", ""),
                                        workspace_id=ws_id,
                                        source_platform="fabric",
                                        source_raw_privilege=action,
                                        source_raw_object={"dar": dar, "item": item},
                                        sync_run_id=self._run_id,
                                    ))

        log.info("Fabric: %d canonical permissions", len(out))
        return out
