"""Structured audit logger — local JSON + optional Azure Monitor custom log."""
from __future__ import annotations
import json
import logging
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)

ACTION_GRANT = "grant"
ACTION_REVOKE = "revoke"
ACTION_SKIP = "skip"
ACTION_CONFLICT = "conflict"
ACTION_ERROR = "error"


class AuditLogger:
    def __init__(self, config: dict, run_id: Optional[str] = None):
        self.run_id = run_id or str(uuid.uuid4())
        obs = config.get("observability", {}) or {}
        log_dir = obs.get("audit_dir", "logs")
        Path(log_dir).mkdir(parents=True, exist_ok=True)
        self._path = Path(log_dir) / f"audit_{self.run_id}.json"
        self._fh = self._path.open("a", encoding="utf-8")
        self._workspace_id = obs.get("log_analytics_workspace_id") or os.getenv(
            "LOG_ANALYTICS_WORKSPACE_ID", "")

    def emit(self, **fields: Any) -> None:
        record = {
            "run_id": self.run_id,
            "event_id": str(uuid.uuid4()),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            **fields,
        }
        try:
            self._fh.write(json.dumps(record, default=str) + "\n")
            self._fh.flush()
        except Exception as e:
            log.warning("audit write failed: %s", e)
        # Azure Monitor ingestion is optional; log only if configured.
        if self._workspace_id:
            log.debug("audit (would ship to LA workspace %s): %s",
                      self._workspace_id, record.get("event_id"))

    def close(self) -> None:
        try:
            self._fh.close()
        except Exception:
            pass
