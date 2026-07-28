from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app
from app.services.pair_apply import ApplyAction, ApplyReport
from app.services.pair_diff import DiffRow


client = TestClient(app)


def test_invalid_fabric_workspace_id_returns_400() -> None:
    response = client.get("/fabric/workspaces/not-a-guid")

    assert response.status_code == 400


def test_invalid_fabric_item_id_returns_400() -> None:
    response = client.get(
        "/fabric/workspaces/43fa6fa9-d863-e811-a838-000d3a309c3d/items/not-a-guid/policies"
    )

    assert response.status_code == 400


def test_invalid_pairing_id_returns_400() -> None:
    response = client.get("/pairings/not-valid/tab/diff")

    assert response.status_code == 400


def test_invalid_databricks_path_segment_returns_400() -> None:
    response = client.get("/databricks/catalog/bad%5Csegment")

    assert response.status_code == 400


def test_invalid_source_type_returns_400() -> None:
    response = client.get("/sources/unknown/sample")

    assert response.status_code == 400


def test_pairing_apply_writes_audit_id(monkeypatch) -> None:
    pairing = {
        "id": "a1b2c3d4e5",
        "label": "Test pairing",
        "dbx_workspace_url": "https://adb.example.azuredatabricks.net",
        "uc_catalog": "main",
        "fabric_workspace_id": "43fa6fa9-d863-e811-a838-000d3a309c3d",
    }
    row = DiffRow(
        principal_key="user@example.com",
        principal_display="user@example.com",
        principal_type="User",
        securable_scope="catalog:main",
        access_class="DATA_READ",
        on_dbx=True,
    )
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

    from app.routers import pairings as pairings_router

    monkeypatch.setattr(pairings_router.pairings_service, "get_pairing", lambda _id: pairing)
    monkeypatch.setattr(
        pairings_router.pair_diff,
        "compute_diff",
        lambda *_args, **_kwargs: {
            "all": [row],
            "dbx_only": [row],
            "fabric_only": [],
            "in_sync": [],
            "errors": [],
        },
    )
    monkeypatch.setattr(pairings_router.pair_apply, "apply_rows", lambda *_args, **_kwargs: report)
    monkeypatch.setattr(pairings_router, "record_permission_apply", lambda **_kwargs: "audit-test")

    response = client.post(
        "/pairings/a1b2c3d4e5/apply",
        data={
            "direction": "dbx_to_fabric",
            "dry_run": "true",
            "row": "user@example.com|catalog:main|DATA_READ",
        },
    )

    assert response.status_code == 200
    assert "audit-test" in response.text


def test_pairing_apply_audit_write_failure_does_not_500(monkeypatch) -> None:
    pairing = {
        "id": "a1b2c3d4e5",
        "label": "Test pairing",
        "dbx_workspace_url": "https://adb.example.azuredatabricks.net",
        "uc_catalog": "main",
        "fabric_workspace_id": "43fa6fa9-d863-e811-a838-000d3a309c3d",
    }
    row = DiffRow(
        principal_key="user@example.com",
        principal_display="user@example.com",
        principal_type="User",
        securable_scope="catalog:main",
        access_class="DATA_READ",
        on_dbx=True,
    )
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
                ok=True,
                message="applied",
            )
        ],
    )

    from app.routers import pairings as pairings_router

    monkeypatch.setattr(pairings_router.pairings_service, "get_pairing", lambda _id: pairing)
    monkeypatch.setattr(
        pairings_router.pair_diff,
        "compute_diff",
        lambda *_args, **_kwargs: {
            "all": [row],
            "dbx_only": [row],
            "fabric_only": [],
            "in_sync": [],
            "errors": [],
        },
    )
    called = {"apply": False}

    def _capture_apply(*_args, **_kwargs):
        called["apply"] = True
        return report

    monkeypatch.setattr(pairings_router.pair_apply, "apply_rows", _capture_apply)

    def _raise_audit_start_error(**_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(pairings_router, "record_permission_apply_start", _raise_audit_start_error)
    monkeypatch.setattr(pairings_router, "record_permission_apply", lambda **_kwargs: "audit-blocked")

    response = client.post(
        "/pairings/a1b2c3d4e5/apply",
        data={
            "direction": "dbx_to_fabric",
            "dry_run": "false",
            "row": "user@example.com|catalog:main|DATA_READ",
        },
    )

    assert response.status_code == 200
    assert "Audit log write failed" in response.text
    assert "real apply blocked" in response.text
    assert called["apply"] is False


def test_pairing_apply_ignores_forged_rows_not_in_live_diff(monkeypatch) -> None:
    pairing = {
        "id": "a1b2c3d4e5",
        "label": "Test pairing",
        "dbx_workspace_url": "https://adb-123.4.azuredatabricks.net",
        "uc_catalog": "main",
        "fabric_workspace_id": "43fa6fa9-d863-e811-a838-000d3a309c3d",
    }
    captured = {}

    from app.routers import pairings as pairings_router

    monkeypatch.setattr(pairings_router.pairings_service, "get_pairing", lambda _id: pairing)
    monkeypatch.setattr(
        pairings_router.pair_diff,
        "compute_diff",
        lambda *_args, **_kwargs: {
            "all": [],
            "dbx_only": [],
            "fabric_only": [],
            "in_sync": [],
            "errors": [],
        },
    )

    def _capture_apply(_pairing, rows, _direction, dry_run=True):
        captured["rows"] = list(rows)
        return ApplyReport(dry_run=dry_run)

    monkeypatch.setattr(pairings_router.pair_apply, "apply_rows", _capture_apply)
    monkeypatch.setattr(pairings_router, "record_permission_apply", lambda **_kwargs: "audit-test")

    response = client.post(
        "/pairings/a1b2c3d4e5/apply",
        data={
            "direction": "dbx_to_fabric",
            "dry_run": "true",
            "row": "attacker@example.com|catalog:main|DATA_ADMIN",
        },
    )

    assert response.status_code == 200
    assert captured["rows"] == []


def test_pairing_apply_without_rows_applies_none(monkeypatch) -> None:
    pairing = {
        "id": "a1b2c3d4e5",
        "label": "Test pairing",
        "dbx_workspace_url": "https://adb-123.4.azuredatabricks.net",
        "uc_catalog": "main",
        "fabric_workspace_id": "43fa6fa9-d863-e811-a838-000d3a309c3d",
    }
    row = DiffRow(
        principal_key="user@example.com",
        principal_display="user@example.com",
        principal_type="User",
        securable_scope="catalog:main",
        access_class="DATA_READ",
        on_dbx=True,
    )
    captured = {}

    from app.routers import pairings as pairings_router

    monkeypatch.setattr(pairings_router.pairings_service, "get_pairing", lambda _id: pairing)
    monkeypatch.setattr(
        pairings_router.pair_diff,
        "compute_diff",
        lambda *_args, **_kwargs: {
            "all": [row],
            "dbx_only": [row],
            "fabric_only": [],
            "in_sync": [],
            "errors": [],
        },
    )

    def _capture_apply(_pairing, rows, _direction, dry_run=True):
        captured["rows"] = list(rows)
        return ApplyReport(dry_run=dry_run)

    monkeypatch.setattr(pairings_router.pair_apply, "apply_rows", _capture_apply)
    monkeypatch.setattr(pairings_router, "record_permission_apply", lambda **_kwargs: "audit-test")

    response = client.post(
        "/pairings/a1b2c3d4e5/apply",
        data={"direction": "dbx_to_fabric", "dry_run": "true"},
    )

    assert response.status_code == 200
    assert captured["rows"] == []


def test_pairing_real_apply_blocks_when_live_diff_fails(monkeypatch) -> None:
    pairing = {
        "id": "a1b2c3d4e5",
        "label": "Test pairing",
        "dbx_workspace_url": "https://adb-123.4.azuredatabricks.net",
        "uc_catalog": "main",
        "fabric_workspace_id": "43fa6fa9-d863-e811-a838-000d3a309c3d",
    }
    called = {"apply": False}

    from app.routers import pairings as pairings_router

    monkeypatch.setattr(pairings_router.pairings_service, "get_pairing", lambda _id: pairing)

    def _raise_diff(*_args, **_kwargs):
        raise RuntimeError("auth failed")

    def _capture_apply(*_args, **_kwargs):
        called["apply"] = True
        return ApplyReport(dry_run=False)

    monkeypatch.setattr(pairings_router.pair_diff, "compute_diff", _raise_diff)
    monkeypatch.setattr(pairings_router.pair_apply, "apply_rows", _capture_apply)
    monkeypatch.setattr(pairings_router, "record_permission_apply", lambda **_kwargs: "audit-test")

    response = client.post(
        "/pairings/a1b2c3d4e5/apply",
        data={
            "direction": "dbx_to_fabric",
            "dry_run": "false",
            "row": "user@example.com|catalog:main|DATA_READ",
        },
    )

    assert response.status_code == 200
    assert called["apply"] is False
    assert "real apply blocked" in response.text
