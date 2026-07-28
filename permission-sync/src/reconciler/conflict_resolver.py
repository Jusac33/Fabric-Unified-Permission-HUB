"""Conflict resolution strategies."""
from __future__ import annotations
import json
import logging
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

import httpx

from src.auth.identity import IdentityProvider
from src.model.canonical_permission import CanonicalPermission
from src.reconciler.diff_engine import PermissionConflict

log = logging.getLogger(__name__)


class ConflictResolver:
    def __init__(self, identity: IdentityProvider, strategy: str = "source_wins",
                 audit_storage_url: str = "", teams_webhook_url: str = ""):
        self._identity = identity
        self._strategy = strategy
        self._audit_url = audit_storage_url
        self._teams_webhook = teams_webhook_url

    def resolve(self, conflict: PermissionConflict) -> Optional[CanonicalPermission]:
        if self._strategy == "source_wins":
            return conflict.source_permission
        if self._strategy == "target_wins":
            return conflict.target_permission
        if self._strategy == "manual":
            self._write_manual(conflict)
            self._notify_teams(conflict)
            return None
        log.warning("Unknown strategy '%s'; defaulting to source_wins", self._strategy)
        return conflict.source_permission

    def resolve_all(self, conflicts: List[PermissionConflict]) -> List[CanonicalPermission]:
        out: List[CanonicalPermission] = []
        for c in conflicts:
            r = self.resolve(c)
            if r is not None:
                out.append(r)
        return out

    def _write_manual(self, conflict: PermissionConflict) -> None:
        record = {
            "conflict_id": conflict.conflict_id,
            "principal_entra_id": conflict.principal_entra_id,
            "securable_fqn": conflict.securable_fqn,
            "securable_type": conflict.securable_type,
            "source_access_class": conflict.source_access_class,
            "target_access_class": conflict.target_access_class,
            "source_platform": conflict.source_platform,
            "target_platform": conflict.target_platform,
            "detected_at": conflict.detected_at.isoformat(),
        }
        # Local artifact (picked up by CI)
        local = Path("logs")
        local.mkdir(exist_ok=True)
        out_path = local / f"conflicts_{conflict.conflict_id}.json"
        out_path.write_text(json.dumps(record, indent=2))
        log.info("Manual conflict written: %s", out_path)

        # Optional: upload to Blob Storage if configured (best-effort, Tier 1 MI)
        if self._audit_url:
            try:
                token = self._identity.get_storage_token()
                r = httpx.put(
                    f"{self._audit_url.rstrip('/')}/conflicts_{conflict.conflict_id}.json",
                    headers={"Authorization": f"Bearer {token}",
                             "x-ms-blob-type": "BlockBlob",
                             "x-ms-version": "2021-12-02",
                             "Content-Type": "application/json"},
                    content=json.dumps(record), timeout=30,
                )
                if r.status_code >= 400:
                    log.warning("Blob upload failed: %s %s", r.status_code, r.text[:200])
            except Exception as e:
                log.warning("Blob upload error: %s", e)

    def _notify_teams(self, conflict: PermissionConflict) -> None:
        if not self._teams_webhook:
            return
        payload = {
            "text": (f"Permission conflict detected: principal={conflict.principal_entra_id} "
                     f"securable={conflict.securable_fqn} "
                     f"source={conflict.source_access_class}/{conflict.source_platform} "
                     f"target={conflict.target_access_class}/{conflict.target_platform}")
        }
        try:
            httpx.post(self._teams_webhook, json=payload, timeout=10)
        except Exception as e:
            log.warning("Teams webhook failed: %s", e)
