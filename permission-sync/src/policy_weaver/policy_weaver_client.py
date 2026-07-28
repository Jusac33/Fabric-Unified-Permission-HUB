"""Policy Weaver facade — routes CanonicalPermissions through our writers.

Auth tier: Tier 2 (API key from Key Vault) if a remote Policy Weaver service
is used. In v1 this module delegates directly to local writers so the full
reconcile works without any external dependency.
"""
from __future__ import annotations
import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from src.auth.identity import IdentityProvider
from src.model.canonical_permission import CanonicalPermission
from src.reconciler.diff_engine import PermissionDiff
from src.translation.privilege_normalizer import PrivilegeNormalizer
from src.writers.fabric_write_planner import FabricWritePlanner
from src.writers.fabric_writer import FabricWriter
from src.writers.unity_catalog_writer import UnityCatalogWriter
from src.writers.snowflake_writer import SnowflakeWriter

log = logging.getLogger(__name__)


@dataclass
class ApplyResult:
    granted: List[Dict] = field(default_factory=list)
    revoked: List[Dict] = field(default_factory=list)
    skipped: List[Dict] = field(default_factory=list)
    errors: List[Dict] = field(default_factory=list)


class PolicyWeaverClient:
    def __init__(self, identity: IdentityProvider, config: dict,
                 normalizer: PrivilegeNormalizer):
        self._identity = identity
        self._config = config
        self._normalizer = normalizer
        fabric_cfg = config.get("discovery", {}).get("fabric", {})
        self._api_base = fabric_cfg.get("api_base", "")
        self._planner = FabricWritePlanner(
            planner_config=config.get("sync", {}).get("fabric_write_planner", {}),
            fabric_config=fabric_cfg,
            normalizer=normalizer,
            dry_run=bool(config.get("sync", {}).get("dry_run", True)),
        )
        self._fabric_writer = FabricWriter(identity, self._api_base)
        self._uc_writer = UnityCatalogWriter(identity)
        self._sf_writer = SnowflakeWriter(identity)
        self._dry_run = bool(config.get("sync", {}).get("dry_run", True))

    def apply_diff(self, diff: PermissionDiff, target_platform: str,
                   ctx: Optional[Dict] = None) -> ApplyResult:
        ctx = ctx or {}
        result = ApplyResult()
        for p in diff.to_grant:
            self._dispatch(p, target_platform, grant=True, ctx=ctx, result=result)
        for p in diff.to_revoke:
            self._dispatch(p, target_platform, grant=False, ctx=ctx, result=result)
        return result

    def _dispatch(self, p: CanonicalPermission, target_platform: str, grant: bool,
                  ctx: Dict, result: ApplyResult) -> None:
        if target_platform == "fabric":
            plan = self._planner.plan(p)
            outcome = (self._fabric_writer.write(plan)
                       if grant else self._fabric_writer.revoke_workspace_role(plan))
        elif target_platform == "unity_catalog":
            priv = self._normalizer.denormalize(p.access_class, "unity_catalog")
            if not priv:
                result.skipped.append({"permission": p.key(), "reason": "no UC mapping"})
                return
            ws_url = ctx.get("databricks_workspace_url", "")
            outcome = (self._uc_writer.grant(p, priv, ws_url) if grant
                       else self._uc_writer.revoke(p, priv, ws_url))
        elif target_platform == "snowflake":
            priv = self._normalizer.denormalize(p.access_class, "snowflake")
            if not priv:
                result.skipped.append({"permission": p.key(), "reason": "no Snowflake mapping"})
                return
            conn = ctx.get("snowflake_connection_params", {})
            outcome = (self._sf_writer.grant(p, priv, conn) if grant
                       else self._sf_writer.revoke(p, priv, conn))
        else:
            result.skipped.append({"permission": p.key(),
                                   "reason": f"unknown target {target_platform}"})
            return

        action = outcome.get("action", "error")
        record = {"permission": p.key(), "outcome": outcome}
        if action == "granted":
            result.granted.append(record)
        elif action == "revoked":
            result.revoked.append(record)
        elif action in ("skipped", "dry_run_would_apply"):
            result.skipped.append(record)
        else:
            result.errors.append(record)
