"""In-process counters; emitted to log at end of run."""
from __future__ import annotations
import logging
from collections import defaultdict
from typing import Dict

log = logging.getLogger(__name__)

METRIC_DISCOVERED = "permissions_discovered_total"
METRIC_GRANTED = "permissions_granted_total"
METRIC_REVOKED = "permissions_revoked_total"
METRIC_SKIPPED = "permissions_skipped_total"
METRIC_CONFLICT = "conflict_total"
METRIC_UNRESOLVED = "identities_unresolved_total"
METRIC_DURATION = "reconcile_duration_seconds"
METRIC_EVENT_ACCEL = "event_accelerations_total"


class MetricsEmitter:
    def __init__(self):
        self._counters: Dict[str, float] = defaultdict(float)

    def inc(self, name: str, value: float = 1.0) -> None:
        self._counters[name] += value

    def set(self, name: str, value: float) -> None:
        self._counters[name] = value

    def snapshot(self) -> Dict[str, float]:
        return dict(self._counters)

    def flush(self) -> None:
        for k, v in self._counters.items():
            log.info("METRIC %s=%s", k, v)
