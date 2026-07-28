from __future__ import annotations

import json

from app.config import settings
from app.services.audit_log import record_permission_apply, record_permission_apply_start
from app.services.pair_apply import ApplyAction, ApplyReport


def test_record_permission_apply_writes_jsonl(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(settings, "AUDIT_DIR", str(tmp_path))
    report = ApplyReport(
        dry_run=True,
        actions=[
            ApplyAction(
                direction="dbx_to_fabric",
                principal="user@example.com",
                securable_scope="catalog:main",
                access_class="DATA_READ",
                target_action="GRANT Viewer",
                layer="workspace_role",
                ok=True,
                message="dry-run",
            )
        ],
    )

    audit_id = record_permission_apply(
        pairing={
            "id": "a1b2c3d4e5",
            "label": "Test",
            "fabric_workspace_id": "43fa6fa9-d863-e811-a838-000d3a309c3d",
            "uc_catalog": "main",
            "dbx_workspace_url": "https://adb.example.azuredatabricks.net",
        },
        direction="dbx_to_fabric",
        dry_run=True,
        selected_count=1,
        report=report,
        error=None,
        actor={"client_host": "testclient", "user_agent": "pytest"},
    )

    path = tmp_path / "permission-applies.jsonl"
    record = json.loads(path.read_text(encoding="utf-8").strip())

    assert record["id"] == audit_id
    assert record["dry_run"] is True
    assert record["counts"] == {"ok": 1, "skipped": 0, "failed": 0}
    assert record["actions"][0]["principal"] == "user@example.com"


def test_record_permission_apply_marks_failed_report_failed(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(settings, "AUDIT_DIR", str(tmp_path))
    report = ApplyReport(
        dry_run=False,
        actions=[
            ApplyAction(
                direction="dbx_to_fabric",
                principal="user@example.com",
                securable_scope="catalog:main",
                access_class="DATA_READ",
                target_action="GRANT Viewer",
                layer="workspace_role",
                ok=False,
                message="403 forbidden",
            )
        ],
    )

    record_permission_apply(
        pairing={
            "id": "a1b2c3d4e5",
            "label": "Test",
            "fabric_workspace_id": "43fa6fa9-d863-e811-a838-000d3a309c3d",
            "uc_catalog": "main",
            "dbx_workspace_url": "https://adb.example.azuredatabricks.net",
        },
        direction="dbx_to_fabric",
        dry_run=False,
        selected_count=1,
        report=report,
        error=None,
        actor={"client_host": "testclient", "user_agent": "pytest"},
    )

    record = json.loads((tmp_path / "permission-applies.jsonl").read_text(encoding="utf-8").strip())

    assert record["status"] == "failed"
    assert record["counts"] == {"ok": 0, "skipped": 0, "failed": 1}


def test_record_permission_apply_start_writes_started_record(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(settings, "AUDIT_DIR", str(tmp_path))

    audit_id = record_permission_apply_start(
        pairing={
            "id": "a1b2c3d4e5",
            "label": "Test",
            "fabric_workspace_id": "43fa6fa9-d863-e811-a838-000d3a309c3d",
            "uc_catalog": "main",
            "dbx_workspace_url": "https://adb.example.azuredatabricks.net",
        },
        direction="dbx_to_fabric",
        dry_run=False,
        selected_count=1,
        actor={"client_host": "testclient", "user_agent": "pytest"},
    )

    record = json.loads((tmp_path / "permission-applies.jsonl").read_text(encoding="utf-8").strip())

    assert record["id"] == audit_id
    assert record["event"] == "permission_apply_started"
    assert record["status"] == "started"
