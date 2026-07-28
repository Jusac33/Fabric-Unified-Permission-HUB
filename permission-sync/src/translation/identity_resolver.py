"""Resolve platform-specific identities to Entra object IDs via Graph API."""
from __future__ import annotations
import logging
from dataclasses import dataclass
from typing import Dict, Optional

import httpx

from src.auth.identity import IdentityProvider

log = logging.getLogger(__name__)

GRAPH = "https://graph.microsoft.com/v1.0"


@dataclass
class EntraIdentity:
    object_id: str
    upn: str
    principal_type: str   # User | Group | ServicePrincipal
    display_name: str


class IdentityResolver:
    def __init__(self, identity: IdentityProvider):
        self._identity = identity
        self._cache: Dict[str, Optional[EntraIdentity]] = {}

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._identity.get_graph_token()}"}

    def _get(self, path: str, params: dict) -> list[dict]:
        try:
            r = httpx.get(f"{GRAPH}{path}", headers=self._headers(), params=params, timeout=20)
            if r.status_code == 404:
                return []
            r.raise_for_status()
            return r.json().get("value", []) or []
        except httpx.HTTPError as e:
            log.warning("Graph %s failed: %s", path, e)
            return []

    def _find_user(self, filter_query: str) -> Optional[EntraIdentity]:
        rows = self._get("/users", {"$filter": filter_query,
                                    "$select": "id,userPrincipalName,displayName,mail"})
        if rows:
            u = rows[0]
            return EntraIdentity(u["id"], u.get("userPrincipalName") or "",
                                 "User", u.get("displayName") or "")
        return None

    def _find_group(self, filter_query: str) -> Optional[EntraIdentity]:
        rows = self._get("/groups", {"$filter": filter_query,
                                     "$select": "id,displayName"})
        if rows:
            g = rows[0]
            return EntraIdentity(g["id"], "", "Group", g.get("displayName") or "")
        return None

    def _find_sp(self, filter_query: str) -> Optional[EntraIdentity]:
        rows = self._get("/servicePrincipals", {"$filter": filter_query,
                                                "$select": "id,displayName,appId"})
        if rows:
            s = rows[0]
            return EntraIdentity(s["id"], "", "ServicePrincipal", s.get("displayName") or "")
        return None

    @staticmethod
    def _escape(val: str) -> str:
        return val.replace("'", "''")

    def resolve_to_entra_id(
        self, platform_identity: str, source_platform: str
    ) -> Optional[EntraIdentity]:
        if not platform_identity:
            return None
        cache_key = f"{source_platform}:{platform_identity.lower()}"
        if cache_key in self._cache:
            return self._cache[cache_key]

        esc = self._escape(platform_identity)
        result: Optional[EntraIdentity] = None

        if source_platform == "unity_catalog":
            result = (self._find_user(f"userPrincipalName eq '{esc}'")
                      or self._find_group(f"displayName eq '{esc}'")
                      or self._find_sp(f"displayName eq '{esc}'"))
        elif source_platform == "snowflake":
            result = (self._find_user(f"mail eq '{esc}'")
                      or self._find_user(f"userPrincipalName eq '{esc}'")
                      or self._find_user(f"displayName eq '{esc}'")
                      or self._find_group(f"displayName eq '{esc}'"))
        elif source_platform == "fabric":
            # Fabric principal IDs are already Entra object IDs
            if self._looks_like_guid(platform_identity):
                result = EntraIdentity(platform_identity, "", "User", "")
            else:
                result = self._find_user(f"userPrincipalName eq '{esc}'")

        if result is None:
            log.warning("Unresolved identity: %s (platform=%s)",
                        platform_identity, source_platform)
        self._cache[cache_key] = result
        return result

    @staticmethod
    def _looks_like_guid(s: str) -> bool:
        import re
        return bool(re.fullmatch(
            r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", s))
