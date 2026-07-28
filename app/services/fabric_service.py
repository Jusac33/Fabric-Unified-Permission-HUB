"""Thin wrapper around policy-weaver's FabricAPI for read-only OneLake security ops."""
from __future__ import annotations
from typing import List, Any, Optional


class FabricService:
    def __init__(self, workspace_id: str, source_type: str = "UNITY_CATALOG"):
        from policyweaver.core.api.fabric import FabricAPI
        from policyweaver.core.enum import PolicyWeaverConnectorType
        try:
            st = PolicyWeaverConnectorType(source_type)
        except Exception:
            st = PolicyWeaverConnectorType.UNITY_CATALOG
        self._api = FabricAPI(workspace_id, st)

    def list_data_access_policies(self, mirror_id: str) -> List[dict]:
        result = self._api.list_data_access_policy(mirror_id)
        return list(result.get("value", [])) if isinstance(result, dict) else []

    def get_workspace_name(self) -> Optional[str]:
        try:
            return self._api.get_workspace_name()
        except Exception:
            return None
