from __future__ import annotations

from dataclasses import dataclass

from app.services import rollback_service
from app.services.audit_log import record_permission_apply


@dataclass
class _Action:
    principal: str
    securable_scope: str
    layer: str
    ok: bool = True
    skipped: bool = False
    message: str = ""


class _Report:
    def __init__(self, actions):
        self.actions = actions
        self.n_ok = sum(1 for a in actions if a.ok and not a.skipped)
        self.n_skipped = sum(1 for a in actions if a.skipped)
        self.n_failed = sum(1 for a in actions if not a.ok and not a.skipped)


def _record(actions, dry_run=False):
    return record_permission_apply(
        pairing={"id": "p1", "label": "x", "fabric_workspace_id": "w", "uc_catalog": "c"},
        direction="dbx_to_fabric",
        dry_run=dry_run,
        selected_count=len(actions),
        report=_Report(actions),
        error=None,
        actor={},
    )


def test_rollback_plan_for_missing_event():
    assert rollback_service.build_plan("does-not-exist") is None


def test_dry_run_has_nothing_to_roll_back():
    aid = _record([_Action("a@x.com", "workspace:w", "workspace_role")], dry_run=True)
    plan = rollback_service.build_plan(aid)
    assert plan["executable"] is False
    assert plan["steps"] == []


def test_reverse_plan_for_applied_actions():
    aid = _record([
        _Action("a@x.com", "workspace:w", "workspace_role", ok=True),
        _Action("b@x.com", "table:c.s.t", "onelake_dar", ok=True),
        _Action("skip@x.com", "schema:c.s", "workspace_role", ok=True, skipped=True),
    ])
    plan = rollback_service.build_plan(aid)
    ops = [s["operation"] for s in plan["steps"]]
    # Skipped action contributes no reverse step.
    assert "remove_workspace_role" in ops
    assert "remove_onelake_dar_member" in ops
    assert len(plan["steps"]) == 2
    # Plan is review-only for safety.
    assert plan["executable"] is False
