"""OneSync Permissions REST client (Tier 2 — API key from Key Vault)."""
from __future__ import annotations
import logging
from dataclasses import dataclass
from typing import List

import httpx

from src.auth.identity import IdentityProvider

log = logging.getLogger(__name__)


@dataclass
class CatalogDescriptor:
    id: str
    name: str
    platform: str   # fabric | unity_catalog | snowflake | ...


@dataclass
class SyncResult:
    source_catalog_id: str
    target_catalog_ids: List[str]
    applied: int
    skipped: int
    errors: List[dict]


class OneSyncClient:
    def __init__(self, identity: IdentityProvider, config: dict):
        self._identity = identity
        cfg = config.get("onesync", {}) or {}
        self._endpoint = ""
        self._api_key = ""
        try:
            self._endpoint = self._identity.get_secret(cfg["api_endpoint_secret"])
            self._api_key = self._identity.get_secret(cfg["api_key_secret"])
        except Exception as e:
            log.warning("OneSync not configured: %s", e)

    def _enabled(self) -> bool:
        return bool(self._endpoint and self._api_key)

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json"}

    def discover_catalogs(self) -> List[CatalogDescriptor]:
        if not self._enabled():
            return []
        try:
            r = httpx.get(f"{self._endpoint.rstrip('/')}/catalogs",
                          headers=self._headers(), timeout=30)
            r.raise_for_status()
            return [CatalogDescriptor(id=c["id"], name=c.get("name", ""),
                                      platform=c.get("platform", ""))
                    for c in r.json().get("value", []) or []]
        except Exception as e:
            log.warning("OneSync discover_catalogs failed: %s", e)
            return []

    def sync_permissions(self, source_catalog_id: str,
                         target_catalog_ids: List[str], dry_run: bool) -> SyncResult:
        if not self._enabled():
            return SyncResult(source_catalog_id, target_catalog_ids, 0, 0,
                              [{"reason": "onesync not configured"}])
        payload = {"source_catalog_id": source_catalog_id,
                   "target_catalog_ids": target_catalog_ids,
                   "dry_run": dry_run}
        try:
            r = httpx.post(f"{self._endpoint.rstrip('/')}/sync",
                           headers=self._headers(), json=payload, timeout=60)
            r.raise_for_status()
            body = r.json()
            return SyncResult(
                source_catalog_id=source_catalog_id,
                target_catalog_ids=target_catalog_ids,
                applied=int(body.get("applied", 0)),
                skipped=int(body.get("skipped", 0)),
                errors=body.get("errors", []) or [],
            )
        except Exception as e:
            return SyncResult(source_catalog_id, target_catalog_ids, 0, 0,
                              [{"exception": str(e)}])
