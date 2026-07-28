"""Persist DBX-workspace ↔ UC-catalog ↔ Fabric-workspace pairings.

Stored in the SQLite database (see ``app/services/db.py``). Legacy
``configs/pairings.json`` is imported once on first DB init. The public function
signatures are unchanged so routers and templates need no modification.
"""
from __future__ import annotations
import uuid
from typing import List, Optional

from app.config import settings
from app.services import db

_PAIRING_COLS = (
    "id, label, dbx_workspace_url, uc_catalog, fabric_workspace_id, notes"
)


def list_pairings() -> List[dict]:
    db.init_db()
    return db.query(
        f"SELECT {_PAIRING_COLS} FROM pairings ORDER BY created_at ASC"
    )


def get_pairing(pairing_id: str) -> Optional[dict]:
    db.init_db()
    return db.query_one(
        f"SELECT {_PAIRING_COLS} FROM pairings WHERE id = ?", (pairing_id,)
    )


def add_pairing(*, label: str, dbx_workspace_url: str, uc_catalog: str,
                fabric_workspace_id: str, notes: str = "") -> dict:
    db.init_db()
    entry = {
        "id": uuid.uuid4().hex[:10],
        "label": (label or "").strip() or f"{uc_catalog} → {fabric_workspace_id[:8]}",
        "dbx_workspace_url": dbx_workspace_url.rstrip("/"),
        "uc_catalog": uc_catalog.strip(),
        "fabric_workspace_id": fabric_workspace_id.strip(),
        "notes": notes.strip(),
    }
    now = db.utcnow()
    db.execute(
        """INSERT INTO pairings
           (id, label, dbx_workspace_url, uc_catalog, fabric_workspace_id,
            notes, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?)""",
        (
            entry["id"], entry["label"], entry["dbx_workspace_url"],
            entry["uc_catalog"], entry["fabric_workspace_id"], entry["notes"],
            now, now,
        ),
    )
    return entry


def delete_pairing(pairing_id: str) -> bool:
    db.init_db()
    cur = db.execute("DELETE FROM pairings WHERE id = ?", (pairing_id,))
    return cur.rowcount > 0


def resolve_dbx_url(pairing_id: Optional[str] = None) -> str:
    """Resolve a DBX workspace URL:
       1. explicit pairing_id → its dbx_workspace_url
       2. settings.DBX_WORKSPACE_URL env var
       3. first pairing if only one is configured
       4. empty string
    """
    if pairing_id:
        p = get_pairing(pairing_id)
        if p:
            return p["dbx_workspace_url"]
    if settings.DBX_WORKSPACE_URL:
        return settings.DBX_WORKSPACE_URL
    entries = list_pairings()
    if len(entries) == 1:
        return entries[0]["dbx_workspace_url"]
    return ""
