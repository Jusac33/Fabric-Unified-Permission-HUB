"""OneSync orchestrator — builds source→target pairs from config and drives sync."""
from __future__ import annotations
import logging
from typing import List

from src.onesync.onesync_client import OneSyncClient, SyncResult

log = logging.getLogger(__name__)


class OneSyncOrchestrator:
    def __init__(self, client: OneSyncClient, config: dict):
        self._client = client
        self._cfg = config
        self._dry_run = bool(config.get("sync", {}).get("dry_run", True))
        self._direction = str(config.get("sync", {}).get("direction", "bidirectional"))

    def run(self) -> List[SyncResult]:
        catalogs = self._client.discover_catalogs()
        if not catalogs:
            log.info("OneSync: no catalogs discovered; skipping")
            return []

        results: List[SyncResult] = []
        # Build pairs per sync.direction
        fabric = [c for c in catalogs if c.platform == "fabric"]
        others = [c for c in catalogs if c.platform != "fabric"]

        def sync(src, tgts):
            if not tgts:
                return
            results.append(self._client.sync_permissions(
                src.id, [t.id for t in tgts], self._dry_run))

        if self._direction in ("source_to_fabric", "bidirectional"):
            for src in others:
                sync(src, fabric)
        if self._direction in ("fabric_to_source", "bidirectional"):
            for src in fabric:
                sync(src, others)
        return results
