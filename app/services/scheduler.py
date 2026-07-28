"""Lightweight periodic re-scan scheduler (stdlib threading, no extra deps).

When enabled (``SCAN_INTERVAL_MINUTES`` > 0), a background daemon thread
periodically recomputes the diff for every configured pairing and records a
drift snapshot. This turns the hub from a manual tool into a monitor without
adding APScheduler/Celery. Disabled by default (interval 0).
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Optional

log = logging.getLogger("uph.scheduler")

_thread: Optional[threading.Thread] = None
_stop = threading.Event()
_last_run: dict = {"at": None, "scanned": 0, "errors": 0}


def _scan_once() -> None:
    from app.services import pairings as pairings_service
    from app.services import pair_diff, drift_service

    scanned = 0
    errors = 0
    for pairing in pairings_service.list_pairings():
        try:
            buckets = pair_diff.compute_diff(
                pairing["dbx_workspace_url"], pairing["uc_catalog"],
                pairing["fabric_workspace_id"], use_cache=False,
            )
            if not buckets.get("errors"):
                drift_service.record_snapshot(pairing["id"], buckets)
                scanned += 1
            else:
                errors += 1
        except Exception as exc:  # noqa: BLE001
            errors += 1
            log.warning("scheduled scan failed for pairing %s: %s",
                        pairing.get("id"), exc)
    _last_run.update(at=time.time(), scanned=scanned, errors=errors)
    log.info("scheduled scan complete: %d scanned, %d errors", scanned, errors)


def _loop(interval_seconds: int) -> None:
    # Stagger first run slightly so startup isn't blocked.
    if _stop.wait(min(30, interval_seconds)):
        return
    while not _stop.is_set():
        try:
            _scan_once()
        except Exception as exc:  # noqa: BLE001
            log.warning("scheduler loop error: %s", exc)
        if _stop.wait(interval_seconds):
            return


def start(interval_minutes: int) -> bool:
    """Start the scheduler thread. Returns True if started, False if disabled."""
    global _thread
    if interval_minutes <= 0:
        log.info("scheduler disabled (SCAN_INTERVAL_MINUTES=%s)", interval_minutes)
        return False
    if _thread and _thread.is_alive():
        return True
    _stop.clear()
    _thread = threading.Thread(
        target=_loop, args=(interval_minutes * 60,), daemon=True, name="uph-scheduler"
    )
    _thread.start()
    log.info("scheduler started: every %d min", interval_minutes)
    return True


def stop() -> None:
    _stop.set()


def status() -> dict:
    return dict(_last_run, running=bool(_thread and _thread.is_alive()))
