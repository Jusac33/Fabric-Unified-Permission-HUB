from __future__ import annotations

from app.services import identity_queue, scheduler
from app.services.audit_log import list_audit_events, record_permission_apply


class _Report:
    n_ok = 2
    n_skipped = 1
    n_failed = 0
    actions = []


def test_identity_queue_enqueue_and_dedupe():
    identity_queue.enqueue(pairing_id="p1", principal="grp-x",
                           principal_type="Group", reason="not found")
    identity_queue.enqueue(pairing_id="p1", principal="grp-x",
                           principal_type="Group", reason="still not found")
    items = identity_queue.list_items(status="unresolved")
    assert len(items) == 1  # deduped by (pairing, principal, source)
    assert items[0]["reason"] == "still not found"


def test_identity_queue_resolve_and_ignore():
    identity_queue.enqueue(pairing_id="p1", principal="a@x.com")
    identity_queue.enqueue(pairing_id="p1", principal="b@x.com")
    items = identity_queue.list_items(status="unresolved")
    identity_queue.resolve(items[0]["id"], "00000000-0000-0000-0000-000000000001")
    identity_queue.ignore(items[1]["id"])
    counts = identity_queue.counts()
    assert counts["resolved"] == 1
    assert counts["ignored"] == 1
    assert counts["unresolved"] == 0


def test_audit_event_written_to_db():
    record_permission_apply(
        pairing={"id": "p1", "label": "x", "fabric_workspace_id": "w", "uc_catalog": "c"},
        direction="dbx_to_fabric",
        dry_run=True,
        selected_count=3,
        report=_Report(),
        error=None,
        actor={"name": "tester"},
    )
    events = list_audit_events(limit=10)
    assert len(events) == 1
    assert events[0]["ok_count"] == 2
    assert events[0]["skipped_count"] == 1
    assert events[0]["dry_run"] == 1


def test_audit_events_filter_by_pairing():
    record_permission_apply(
        pairing={"id": "pA", "label": "a", "fabric_workspace_id": "w", "uc_catalog": "c"},
        direction="dbx_to_fabric", dry_run=True, selected_count=1,
        report=_Report(), error=None, actor={},
    )
    record_permission_apply(
        pairing={"id": "pB", "label": "b", "fabric_workspace_id": "w", "uc_catalog": "c"},
        direction="dbx_to_fabric", dry_run=True, selected_count=1,
        report=_Report(), error=None, actor={},
    )
    assert len(list_audit_events(pairing_id="pA")) == 1
    assert len(list_audit_events(pairing_id="pB")) == 1
    assert len(list_audit_events()) == 2


def test_scheduler_disabled_by_default():
    started = scheduler.start(0)
    assert started is False
    assert scheduler.status()["running"] is False
