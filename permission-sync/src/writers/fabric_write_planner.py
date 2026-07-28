"""The SINGLE place that decides which Fabric security layer to write."""
from __future__ import annotations
import logging
from dataclasses import dataclass
from typing import Optional

from src.model.canonical_permission import CanonicalPermission
from src.translation.privilege_normalizer import PrivilegeNormalizer

log = logging.getLogger(__name__)


@dataclass
class FabricWritePlan:
    layer: str                           # workspace_role | onelake_data_access_role | skip
    workspace_id: str
    item_id: Optional[str]
    principal_entra_id: str
    principal_type: str
    role_or_action: str
    dry_run: bool = True


class FabricWritePlanner:
    def __init__(self, planner_config: dict, fabric_config: dict,
                 normalizer: PrivilegeNormalizer, dry_run: bool = True):
        self._cfg = planner_config or {}
        self._onelake_enabled = bool(fabric_config.get("onelake_data_access_roles_enabled", False))
        self._normalizer = normalizer
        self._dry_run = dry_run

    def plan(self, permission: CanonicalPermission) -> FabricWritePlan:
        ac_cfg = self._cfg.get(permission.access_class) or {}
        primary = ac_cfg.get("primary_layer", "workspace_role")
        fallback = ac_cfg.get("fallback_layer", "workspace_role")

        layer = primary
        if primary == "onelake_data_access_role" and not self._onelake_enabled:
            log.info("OneLake DAR disabled; falling back to %s for %s",
                     fallback, permission.access_class)
            layer = fallback

        if layer == "none":
            log.warning("No Fabric equivalent for %s — skipping", permission.access_class)
            return FabricWritePlan(
                layer="skip", workspace_id=permission.workspace_id, item_id=None,
                principal_entra_id=permission.principal_entra_id,
                principal_type=permission.principal_type,
                role_or_action="", dry_run=self._dry_run,
            )

        target_table = ("fabric_onelake_role"
                        if layer == "onelake_data_access_role" else "fabric_workspace_role")
        target_priv = self._normalizer.denormalize(permission.access_class, target_table)
        if not target_priv:
            return FabricWritePlan(
                layer="skip", workspace_id=permission.workspace_id, item_id=None,
                principal_entra_id=permission.principal_entra_id,
                principal_type=permission.principal_type,
                role_or_action="", dry_run=self._dry_run,
            )

        item_id = None
        if layer == "onelake_data_access_role":
            # For OneLake DAR, expect a lakehouse-scoped permission
            item_id = (permission.source_raw_object.get("item") or {}).get("id") \
                if isinstance(permission.source_raw_object, dict) else None

        return FabricWritePlan(
            layer=layer,
            workspace_id=permission.workspace_id,
            item_id=item_id,
            principal_entra_id=permission.principal_entra_id,
            principal_type=permission.principal_type,
            role_or_action=target_priv,
            dry_run=self._dry_run,
        )
