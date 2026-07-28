"""Unity Catalog discovery via Databricks REST + MI-issued AAD token."""
from __future__ import annotations
import logging
from typing import List, Optional
from uuid import uuid4

import httpx

from src.auth.identity import IdentityProvider
from src.model.canonical_permission import CanonicalPermission
from src.translation.privilege_normalizer import PrivilegeNormalizer
from src.translation.identity_resolver import IdentityResolver

log = logging.getLogger(__name__)


class UnityCatalogDiscovery:
    """Auth tier: Tier 1 — MI/DefaultAzureCredential issuing Databricks AAD token."""

    def __init__(self, config: dict, identity: IdentityProvider,
                 normalizer: PrivilegeNormalizer,
                 resolver: IdentityResolver, run_id: Optional[str] = None):
        self._cfg = config.get("discovery", {}).get("unity_catalog", {})
        self._account_host = (self._cfg.get("databricks_account_host") or "").rstrip("/")
        self._identity = identity
        self._normalizer = normalizer
        self._resolver = resolver
        self._run_id = run_id or uuid4().hex

    def _ws_headers(self) -> dict:
        return {"Authorization": f"Bearer {self._identity.get_databricks_token()}"}

    def _acct_get(self, path: str) -> dict:
        r = httpx.get(f"{self._account_host}{path}", headers=self._ws_headers(), timeout=60)
        r.raise_for_status()
        return r.json()

    def _ws_get(self, workspace_url: str, path: str, params: Optional[dict] = None) -> dict:
        r = httpx.get(f"{workspace_url.rstrip('/')}{path}",
                      headers=self._ws_headers(), params=params, timeout=60)
        if r.status_code in (401, 403):
            log.warning("DBX %s %s: %d", workspace_url, path, r.status_code)
            return {}
        r.raise_for_status()
        return r.json()

    def _list_workspaces(self) -> List[dict]:
        account_id_secret = self._cfg.get("databricks_account_id_secret")
        if not account_id_secret:
            log.warning("No databricks_account_id_secret set; cannot list workspaces")
            return []
        try:
            account_id = self._identity.get_secret(account_id_secret)
        except RuntimeError as e:
            log.warning("Databricks account ID lookup failed: %s", e)
            return []
        try:
            return self._acct_get(f"/api/2.0/accounts/{account_id}/workspaces") or []
        except httpx.HTTPError as e:
            log.warning("Account workspaces list failed: %s", e)
            return []

    def discover_all(self, only_workspace_url: Optional[str] = None
                     ) -> List[CanonicalPermission]:
        out: List[CanonicalPermission] = []
        workspaces = (
            [{"deployment_name": "", "workspace_url": only_workspace_url}]
            if only_workspace_url else self._list_workspaces()
        )
        log.info("UC: %d workspace(s) to discover", len(workspaces))

        for ws in workspaces:
            ws_url = ws.get("workspace_url") or (
                f"https://{ws.get('deployment_name')}.cloud.databricks.com"
                if ws.get("deployment_name") else ""
            )
            if not ws_url:
                continue
            out.extend(self._discover_workspace(ws_url))

        log.info("UC: %d canonical permissions", len(out))
        return out

    def _discover_workspace(self, ws_url: str) -> List[CanonicalPermission]:
        out: List[CanonicalPermission] = []
        catalogs = (self._ws_get(ws_url, "/api/2.1/unity-catalog/catalogs").get("catalogs") or [])
        for cat in catalogs:
            cn = cat.get("name")
            if not cn:
                continue
            out.extend(self._grants_to_canonical(
                ws_url, "catalog", cn, {"catalog": cn}, ws_url))
            schemas = (self._ws_get(
                ws_url, "/api/2.1/unity-catalog/schemas",
                {"catalog_name": cn}).get("schemas") or [])
            for sch in schemas:
                sn = sch.get("name")
                if not sn or sn == "information_schema":
                    continue
                full_schema = f"{cn}.{sn}"
                out.extend(self._grants_to_canonical(
                    ws_url, "schema", full_schema,
                    {"catalog": cn, "schema": sn}, ws_url))
                tables = (self._ws_get(
                    ws_url, "/api/2.1/unity-catalog/tables",
                    {"catalog_name": cn, "schema_name": sn, "max_results": 1000}
                ).get("tables") or [])
                for t in tables:
                    tn = t.get("name")
                    if not tn:
                        continue
                    full_table = f"{full_schema}.{tn}"
                    out.extend(self._grants_to_canonical(
                        ws_url, "table", full_table,
                        {"catalog": cn, "schema": sn, "table": tn}, ws_url))
        return out

    def _grants_to_canonical(self, ws_url: str, securable_type: str, full_name: str,
                             _sec_parts: dict, workspace_id: str) -> List[CanonicalPermission]:
        path = f"/api/2.1/unity-catalog/permissions/{securable_type}/{full_name}"
        body = self._ws_get(ws_url, path)
        out: List[CanonicalPermission] = []
        for pa in body.get("privilege_assignments") or []:
            principal = pa.get("principal", "")
            ident = self._resolver.resolve_to_entra_id(principal, "unity_catalog")
            if not ident:
                continue
            for privilege in pa.get("privileges") or []:
                access = self._normalizer.normalize(privilege, "unity_catalog")
                if not access:
                    continue
                out.append(CanonicalPermission(
                    principal_entra_id=ident.object_id,
                    principal_type=ident.principal_type,
                    principal_display_name=ident.display_name or principal,
                    access_class=access,
                    securable_type=securable_type,
                    securable_fqn=full_name,
                    workspace_id=workspace_id,
                    source_platform="unity_catalog",
                    source_raw_privilege=privilege,
                    source_raw_object=pa,
                    sync_run_id=self._run_id,
                ))
        return out
