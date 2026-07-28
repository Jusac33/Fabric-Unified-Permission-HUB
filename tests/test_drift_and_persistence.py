from __future__ import annotations

from app.services import db, drift_service, pairings as pairings_service


def _row(principal, scope, access="DATA_READ", on_dbx=True, on_fabric=False):
    return {
        "principal_key": principal,
        "principal_display": principal,
        "principal_type": "User",
        "securable_scope": scope,
        "access_class": access,
        "constraint_kind": "",
        "constraint_key": "",
        "on_dbx": on_dbx,
        "on_fabric": on_fabric,
    }


def _buckets(rows):
    return {
        "all": rows,
        "dbx_only": [r for r in rows if r["on_dbx"] and not r["on_fabric"]],
        "fabric_only": [r for r in rows if r["on_fabric"] and not r["on_dbx"]],
        "in_sync": [r for r in rows if r["on_dbx"] and r["on_fabric"]],
    }


def test_first_snapshot_marks_is_first():
    drift = drift_service.record_snapshot("p1", _buckets([_row("a@x.com", "catalog:main")]))
    assert drift["is_first"] is True
    assert drift["added"] == []
    assert drift["removed"] == []


def test_second_identical_snapshot_has_no_drift():
    rows = [_row("a@x.com", "catalog:main")]
    drift_service.record_snapshot("p1", _buckets(rows))
    drift = drift_service.record_snapshot("p1", _buckets(rows))
    assert drift["is_first"] is False
    assert drift["added"] == []
    assert drift["removed"] == []
    assert drift["unchanged_since_previous"] is True


def test_drift_detects_added_and_removed():
    drift_service.record_snapshot("p1", _buckets([_row("a@x.com", "catalog:main")]))
    drift = drift_service.record_snapshot(
        "p1", _buckets([_row("b@x.com", "catalog:main")])
    )
    assert drift["is_first"] is False
    added_keys = {r["principal_key"] for r in drift["added"]}
    removed_keys = {r["principal_key"] for r in drift["removed"]}
    assert added_keys == {"b@x.com"}
    assert removed_keys == {"a@x.com"}


def test_snapshots_are_isolated_per_pairing():
    drift_service.record_snapshot("p1", _buckets([_row("a@x.com", "catalog:main")]))
    drift = drift_service.record_snapshot("p2", _buckets([_row("z@x.com", "catalog:other")]))
    # p2's first snapshot is independent of p1.
    assert drift["is_first"] is True


def test_history_returns_recent_first():
    drift_service.record_snapshot("p1", _buckets([_row("a@x.com", "catalog:main")]))
    drift_service.record_snapshot("p1", _buckets([_row("a@x.com", "catalog:main"),
                                                  _row("b@x.com", "schema:main.gold")]))
    hist = drift_service.snapshot_history("p1")
    assert len(hist) == 2
    assert hist[0]["row_count"] == 2  # most recent first


def test_pairing_persists_across_connection_reset():
    pairings_service.add_pairing(
        label="t", dbx_workspace_url="https://adb-1.azuredatabricks.net",
        uc_catalog="main", fabric_workspace_id="11111111-1111-1111-1111-111111111111",
    )
    # Simulate a new thread/connection.
    if hasattr(db._local, "conn"):
        db._local.conn.close()
        del db._local.conn
    db._initialized = False
    assert any(p["uc_catalog"] == "main" for p in pairings_service.list_pairings())
