from __future__ import annotations

from app.services import approvals


def test_create_and_get_request():
    aid = approvals.create_request(
        pairing_id="p1", direction="dbx_to_fabric",
        row_keys=["a|catalog:main|DATA_READ||", "b|schema:main.gold|DATA_READ||"],
        requested_by="tester", note="please review",
    )
    req = approvals.get(aid)
    assert req["status"] == "pending"
    assert req["pairing_id"] == "p1"
    assert len(req["row_keys"]) == 2


def test_approve_then_execute_flow():
    aid = approvals.create_request(pairing_id="p1", direction="dbx_to_fabric",
                                   row_keys=["x"], requested_by="t")
    assert approvals.pending_count() == 1
    assert approvals.decide(aid, approved=True, decided_by="approver") is True
    assert approvals.get(aid)["status"] == "approved"
    assert approvals.pending_count() == 0
    approvals.mark_executed(aid)
    assert approvals.get(aid)["status"] == "executed"


def test_reject_request():
    aid = approvals.create_request(pairing_id="p1", direction="dbx_to_fabric",
                                   row_keys=["x"])
    assert approvals.decide(aid, approved=False) is True
    assert approvals.get(aid)["status"] == "rejected"


def test_cannot_decide_twice():
    aid = approvals.create_request(pairing_id="p1", direction="dbx_to_fabric",
                                   row_keys=["x"])
    assert approvals.decide(aid, approved=True) is True
    # Second decision on a non-pending request is a no-op.
    assert approvals.decide(aid, approved=False) is False


def test_list_filter_by_status():
    approvals.create_request(pairing_id="p1", direction="dbx_to_fabric", row_keys=["x"])
    aid2 = approvals.create_request(pairing_id="p2", direction="dbx_to_fabric", row_keys=["y"])
    approvals.decide(aid2, approved=True)
    assert len(approvals.list_requests(status="pending")) == 1
    assert len(approvals.list_requests(status="approved")) == 1
    assert len(approvals.list_requests()) == 2
