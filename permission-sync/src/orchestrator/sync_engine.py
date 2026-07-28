"""Sync engine — central orchestrator.

Phases (spec):
  1. STARTUP          — load config, auth, build clients
  2. FULL RECONCILE   — discover source + target, diff, resolve conflicts, apply
  3. EVENT ACCELERATOR — gated off unless event_accelerators.enabled=true
"""
from __future__ import annotations
import argparse
import asyncio
import logging
import time
import uuid
from typing import List

from src.auth.identity import IdentityProvider
from src.config.config_loader import load_config
from src.discovery.fabric_discovery import FabricDiscovery
from src.discovery.snowflake_discovery import SnowflakeDiscovery
from src.discovery.unity_catalog_discovery import UnityCatalogDiscovery
from src.model.canonical_permission import CanonicalPermission
from src.observability.audit_logger import (
    ACTION_CONFLICT, ACTION_GRANT, ACTION_REVOKE, AuditLogger,
)
from src.observability.metrics_emitter import (
    METRIC_CONFLICT, METRIC_DISCOVERED, METRIC_DURATION, METRIC_GRANTED,
    METRIC_REVOKED, METRIC_SKIPPED, MetricsEmitter,
)
from src.onesync.onesync_client import OneSyncClient
from src.onesync.onesync_orchestrator import OneSyncOrchestrator
from src.policy_weaver.policy_weaver_client import PolicyWeaverClient
from src.reconciler.conflict_resolver import ConflictResolver
from src.reconciler.diff_engine import PermissionDiff, compute_diff
from src.translation.identity_resolver import IdentityResolver
from src.translation.privilege_normalizer import PrivilegeNormalizer

log = logging.getLogger(__name__)


class SyncEngine:
    def __init__(self, config_path: str, mode: str = "full_reconcile"):
        self.config = load_config(config_path)
        self.mode = mode
        self.run_id = uuid.uuid4().hex
        self.identity = IdentityProvider()
        self.normalizer = PrivilegeNormalizer(
            self.config.get("privilege_matrix_path", "config/privilege_matrix.yaml"))
        self.resolver = IdentityResolver(self.identity)
        self.audit = AuditLogger(self.config, run_id=self.run_id)
        self.metrics = MetricsEmitter()

        sync_cfg = self.config.get("sync", {}) or {}
        self.dry_run = bool(sync_cfg.get("dry_run", True))
        self.direction = str(sync_cfg.get("direction", "source_to_fabric"))
        self.source_platform = (sync_cfg.get("source_platform") or "").lower()

        cr_cfg = self.config.get("conflict_resolution", {}) or {}
        self.conflict_resolver = ConflictResolver(
            identity=self.identity,
            strategy=cr_cfg.get("strategy", "source_wins"),
            audit_storage_url=cr_cfg.get("audit_storage_url", ""),
            teams_webhook_url=cr_cfg.get("teams_webhook_url", ""),
        )
        self.pw = PolicyWeaverClient(self.identity, self.config, self.normalizer)
        self.onesync = OneSyncOrchestrator(
            OneSyncClient(self.identity, self.config), self.config)

    # ---------- discovery ----------
    def _discover_source_sync(self) -> List[CanonicalPermission]:
        if self.source_platform == "unity_catalog":
            return UnityCatalogDiscovery(self.config, self.identity,
                                         self.normalizer, self.resolver,
                                         self.run_id).discover_all()
        if self.source_platform == "snowflake":
            return SnowflakeDiscovery(self.config, self.identity, self.normalizer,
                                      self.resolver, self.run_id).discover_all()
        log.warning("Unknown source_platform '%s'; returning []", self.source_platform)
        return []

    def _discover_target_sync(self) -> List[CanonicalPermission]:
        return FabricDiscovery(self.config, self.identity, self.normalizer,
                               self.run_id).discover_all()

    async def _discover(self):
        return await asyncio.gather(
            asyncio.to_thread(self._discover_source_sync),
            asyncio.to_thread(self._discover_target_sync),
        )

    # ---------- reconcile ----------
    async def full_reconcile(self) -> dict:
        start = time.time()
        source, target = await self._discover()
        self.metrics.inc(METRIC_DISCOVERED, len(source) + len(target))
        log.info("discovered: source=%d target=%d", len(source), len(target))

        if self.direction == "fabric_to_source":
            desired, current, target_platform = target, source, self.source_platform
        else:
            if self.direction == "bidirectional":
                log.warning("bidirectional not supported in v1; using source_to_fabric")
            desired, current, target_platform = source, target, "fabric"

        diff: PermissionDiff = compute_diff(current_state=current, desired_state=desired)
        self.metrics.inc(METRIC_CONFLICT, len(diff.conflicts))

        for c in diff.conflicts:
            self.audit.emit(action=ACTION_CONFLICT,
                            principal=c.principal_entra_id,
                            securable=c.securable_fqn,
                            source_access_class=c.source_access_class,
                            target_access_class=c.target_access_class)

        winners = self.conflict_resolver.resolve_all(diff.conflicts)
        existing_grant_keys = {p.key() for p in diff.to_grant}
        for w in winners:
            if w.key() not in existing_grant_keys:
                diff.to_grant.append(w)

        applied = self.pw.apply_diff(diff, target_platform=target_platform)
        self.metrics.inc(METRIC_GRANTED, len(applied.granted))
        self.metrics.inc(METRIC_REVOKED, len(applied.revoked))
        self.metrics.inc(METRIC_SKIPPED, len(applied.skipped))

        for p in diff.to_grant:
            self.audit.emit(action=ACTION_GRANT, dry_run=self.dry_run,
                            principal=p.principal_entra_id,
                            securable=p.securable_fqn,
                            access_class=p.access_class,
                            target_platform=target_platform)
        for p in diff.to_revoke:
            self.audit.emit(action=ACTION_REVOKE, dry_run=self.dry_run,
                            principal=p.principal_entra_id,
                            securable=p.securable_fqn,
                            access_class=p.access_class,
                            target_platform=target_platform)

        onesync_results = []
        if self.config.get("onesync", {}).get("enabled", False):
            onesync_results = self.onesync.run()

        duration = time.time() - start
        self.metrics.set(METRIC_DURATION, duration)
        self.metrics.flush()
        self.audit.close()

        return {
            "run_id": self.run_id,
            "mode": self.mode,
            "direction": self.direction,
            "source_platform": self.source_platform,
            "target_platform": target_platform,
            "source_count": len(source),
            "target_count": len(target),
            "to_grant": len(diff.to_grant),
            "to_revoke": len(diff.to_revoke),
            "conflicts": len(diff.conflicts),
            "granted": len(applied.granted),
            "revoked": len(applied.revoked),
            "skipped": len(applied.skipped),
            "errors": len(applied.errors),
            "onesync_results": len(onesync_results),
            "duration_seconds": round(duration, 2),
            "dry_run": self.dry_run,
        }

    async def run(self) -> dict:
        if self.mode == "event_accelerator" and not self.config.get(
                "event_accelerators", {}).get("enabled", False):
            log.info("event_accelerators disabled; falling back to full_reconcile")
        return await self.full_reconcile()


def _parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--config", default="config/settings.yaml")
    p.add_argument("--mode", default="full_reconcile",
                   choices=["full_reconcile", "event_accelerator"])
    return p.parse_args()


def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s - %(message)s")
    args = _parse_args()
    engine = SyncEngine(args.config, args.mode)
    result = asyncio.run(engine.run())
    log.info("RUN SUMMARY: %s", result)


if __name__ == "__main__":
    main()
