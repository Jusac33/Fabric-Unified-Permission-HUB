from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import threading

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from app.config import settings
from app.services import approvals
from app.services import drift_service
from app.services import identity_queue
from app.services import pairings as pairings_service
from app.services import pair_diff
from app.services import cache as _cache
from app.services.audit_log import list_audit_events
from app.services.fabric_rest import get_workspace
from app.services.databricks_rest import DatabricksUCClient

router = APIRouter()


def _enrich_pairing(p: dict) -> dict:
    item = dict(p)
    try:
        ws = get_workspace(p["fabric_workspace_id"])
        item["fabric_name"] = (ws or {}).get("displayName") or ""
    except Exception as e:
        item["fabric_name"] = ""
        item["fabric_error"] = str(e)
    item["uc_catalog_error"] = ""
    try:
        c = DatabricksUCClient(p["dbx_workspace_url"])
        names = {cat.get("name") for cat in c.list_catalogs()}
        item["uc_catalog_exists"] = p["uc_catalog"] in names
    except Exception as e:
        item["uc_catalog_exists"] = None
        item["uc_catalog_error"] = str(e)
    return item


def _relative_age(iso_timestamp: str | None) -> str:
    """Human-friendly age for an ISO-8601 timestamp ('4m ago')."""
    if not iso_timestamp:
        return ""
    try:
        moment = datetime.fromisoformat(str(iso_timestamp))
    except ValueError:
        return ""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    seconds = (datetime.now(timezone.utc) - moment).total_seconds()
    if seconds < 60:
        return "just now"
    for size, suffix in ((3600, "m"), (86400, "h"), (None, "d")):
        if size is None:
            return f"{int(seconds // 86400)}d ago"
        if seconds < size:
            return f"{int(seconds // (size // 60))}{suffix} ago"
    return ""


def _attach_snapshots(enriched: list[dict]) -> None:
    """Decorate each pairing with its most recent drift snapshot (best-effort)."""
    for item in enriched:
        snapshot = None
        try:
            snapshot = drift_service.latest_snapshot(item["id"])
        except Exception:
            snapshot = None
        item["snapshot"] = snapshot
        item["snapshot_age"] = _relative_age((snapshot or {}).get("taken_at"))
        total = int((snapshot or {}).get("row_count") or 0)
        in_sync = int((snapshot or {}).get("in_sync") or 0)
        item["aligned_pct"] = round(in_sync * 100 / total) if total else None
        item["healthy"] = bool(
            item.get("fabric_name")
            and item.get("uc_catalog_exists") is True
            and not item.get("fabric_error")
            and not item.get("uc_catalog_error")
        )


def _dashboard_stats(enriched: list[dict]) -> dict:
    """Aggregate headline numbers for the dashboard tiles."""
    totals = {"rows": 0, "in_sync": 0, "dbx_only": 0, "fabric_only": 0}
    scanned = 0
    for item in enriched:
        snapshot = item.get("snapshot")
        if not snapshot:
            continue
        scanned += 1
        totals["rows"] += int(snapshot.get("row_count") or 0)
        totals["in_sync"] += int(snapshot.get("in_sync") or 0)
        totals["dbx_only"] += int(snapshot.get("dbx_only") or 0)
        totals["fabric_only"] += int(snapshot.get("fabric_only") or 0)

    rows = totals["rows"]
    try:
        pending_approvals = approvals.pending_count()
    except Exception:
        pending_approvals = 0
    try:
        unresolved_identities = identity_queue.counts().get("unresolved", 0)
    except Exception:
        unresolved_identities = 0
    try:
        recent_audit = list_audit_events(limit=6)
    except Exception:
        recent_audit = []
    for event in recent_audit:
        event["age"] = _relative_age(event.get("timestamp"))

    return {
        "pairings": len(enriched),
        "connected": sum(1 for i in enriched if i.get("healthy")),
        "attention": sum(1 for i in enriched if not i.get("healthy")),
        "scanned": scanned,
        "rows": rows,
        "in_sync": totals["in_sync"],
        "dbx_only": totals["dbx_only"],
        "fabric_only": totals["fabric_only"],
        "drift": totals["dbx_only"] + totals["fabric_only"],
        "aligned_pct": round(totals["in_sync"] * 100 / rows) if rows else None,
        "in_sync_pct": round(totals["in_sync"] * 100 / rows, 1) if rows else 0,
        "dbx_only_pct": round(totals["dbx_only"] * 100 / rows, 1) if rows else 0,
        "fabric_only_pct": round(totals["fabric_only"] * 100 / rows, 1) if rows else 0,
        "pending_approvals": pending_approvals,
        "unresolved_identities": unresolved_identities,
        "recent_audit": recent_audit,
    }


@router.get("/", response_class=HTMLResponse)
def index(request: Request):
    return request.app.state.templates.TemplateResponse(
        request, "index.html", {"settings": settings},
    )


@router.get("/_data", response_class=HTMLResponse)
def index_data(request: Request, refresh: int = 0):
    pairings = pairings_service.list_pairings()
    cache_key = "home::pairings::" + ",".join(p["id"] for p in pairings)
    if refresh:
        _cache.invalidate(cache_key)
    enriched = _cache.get(cache_key)
    if enriched is None:
        if pairings:
            with ThreadPoolExecutor(max_workers=min(8, len(pairings))) as ex:
                enriched = list(ex.map(_enrich_pairing, pairings))
        else:
            enriched = []
        # Only cache a fully-successful enrichment. Caching transient errors
        # (e.g. a not-yet-warm Databricks token at first load) would pin a stale
        # "error" badge for the full TTL.
        had_error = any(
            item.get("fabric_error") or item.get("uc_catalog_error")
            for item in enriched
        )
        if not had_error:
            _cache.set(cache_key, enriched, ttl=300.0)

    # Fire-and-forget: pre-warm diff cache so the detail page is instant
    warm_key = "warm::" + cache_key
    if pairings and _cache.get(warm_key) is None:
        _cache.set(warm_key, True, ttl=300.0)

        def _warm():
            for p in pairings:
                try:
                    pair_diff.compute_diff(
                        p["dbx_workspace_url"], p["uc_catalog"],
                        p["fabric_workspace_id"], use_cache=True,
                    )
                except Exception:
                    pass
        threading.Thread(target=_warm, daemon=True).start()

    # Snapshot/stat decoration is DB-local and cheap, so it stays outside the
    # REST cache and always reflects the newest scan.
    enriched = [dict(item) for item in enriched]
    _attach_snapshots(enriched)

    return request.app.state.templates.TemplateResponse(
        request, "_home_data.html",
        {
            "settings": settings,
            "pairings": enriched,
            "stats": _dashboard_stats(enriched),
        },
    )
