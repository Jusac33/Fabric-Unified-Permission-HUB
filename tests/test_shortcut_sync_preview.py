from __future__ import annotations

from app.services import fabric_rest, shortcut_sync_preview
from app.services.source_capabilities import SourcePlatform, TranslationStatus


def test_shortcut_preview_blocks_s3_writes(monkeypatch) -> None:
    monkeypatch.setattr(
        fabric_rest,
        "list_workspace_shortcuts",
        lambda _workspace_id: [
            {
                "item": {"id": "lakehouse-id", "type": "Lakehouse", "displayName": "LH"},
                "shortcut": {
                    "name": "sales",
                    "path": "Tables/sales",
                    "target": {
                        "amazonS3": {
                            "bucketName": "company-sales",
                            "subpath": "/curated",
                            "connectionId": "conn",
                        }
                    },
                },
            }
        ],
    )

    rows = shortcut_sync_preview.preview_workspace_shortcuts(
        "43fa6fa9-d863-e811-a838-000d3a309c3d"
    )

    assert len(rows) == 1
    assert rows[0].target_platform == SourcePlatform.AWS_LAKE_FORMATION_S3
    assert rows[0].grant_sync == TranslationStatus.PLANNED
    assert rows[0].applyable is False
    assert "preview-only" in rows[0].blocked_reason


def test_shortcut_preview_marks_onelake_shortcut_discovery_only(monkeypatch) -> None:
    monkeypatch.setattr(
        fabric_rest,
        "list_workspace_shortcuts",
        lambda _workspace_id: [
            {
                "item": {"id": "lakehouse-id", "type": "Lakehouse", "displayName": "LH"},
                "shortcut": {
                    "name": "internal",
                    "path": "Files/internal",
                    "target": {
                        "oneLake": {
                            "workspaceId": "workspace-id",
                            "itemId": "item-id",
                            "path": "Files/raw",
                        }
                    },
                },
            }
        ],
    )

    rows = shortcut_sync_preview.preview_workspace_shortcuts(
        "43fa6fa9-d863-e811-a838-000d3a309c3d"
    )

    assert rows[0].target_platform == SourcePlatform.FABRIC_ONELAKE
    assert rows[0].grant_sync == TranslationStatus.LIVE
    assert rows[0].row_column_sync == TranslationStatus.DISCOVERY_ONLY
    assert rows[0].applyable is False
    assert "row/column policy translation" in rows[0].blocked_reason
