"""Read-only preview planning for Fabric shortcuts and external authorities."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.services import fabric_rest
from app.services.source_capabilities import (
    SourcePlatform,
    TranslationStatus,
    shortcut_translation_plan,
)


@dataclass(frozen=True)
class ShortcutPreviewRow:
    workspace_id: str
    item_id: str
    item_name: str
    item_type: str
    shortcut_name: str
    shortcut_path: str
    target_platform: SourcePlatform
    target_type: str
    target_location: str
    grant_sync: TranslationStatus
    row_column_sync: TranslationStatus
    bidirectional: bool
    applyable: bool
    blocked_reason: str
    notes: str
    raw_shortcut: dict[str, Any]


def preview_workspace_shortcuts(workspace_id: str) -> list[ShortcutPreviewRow]:
    rows: list[ShortcutPreviewRow] = []
    for entry in fabric_rest.list_workspace_shortcuts(workspace_id):
        item = entry.get("item") or {}
        shortcut = entry.get("shortcut") or {}
        plan = shortcut_translation_plan(shortcut)
        target = plan["target"]
        applyable = (
            plan["grant_sync"] == TranslationStatus.LIVE
            and plan["row_column_sync"] == TranslationStatus.LIVE
            and target.platform in {SourcePlatform.DATABRICKS_UC, SourcePlatform.FABRIC_ONELAKE}
        )
        blocked_reason = "" if applyable else _blocked_reason(plan)
        rows.append(
            ShortcutPreviewRow(
                workspace_id=workspace_id,
                item_id=item.get("id") or "",
                item_name=item.get("displayName") or "",
                item_type=item.get("type") or "",
                shortcut_name=plan["shortcut_name"],
                shortcut_path=plan["shortcut_path"],
                target_platform=target.platform,
                target_type=target.target_type,
                target_location=target.location,
                grant_sync=plan["grant_sync"],
                row_column_sync=plan["row_column_sync"],
                bidirectional=plan["bidirectional"],
                applyable=applyable,
                blocked_reason=blocked_reason,
                notes=plan["notes"],
                raw_shortcut=shortcut,
            )
        )
    return rows


def _blocked_reason(plan: dict[str, Any]) -> str:
    grant_sync = plan["grant_sync"]
    row_column_sync = plan["row_column_sync"]
    target = plan["target"]
    if grant_sync == TranslationStatus.PLANNED:
        return f"{target.platform.value} connector/writer is planned; preview-only for now"
    if grant_sync == TranslationStatus.DISCOVERY_ONLY:
        return "target permissions are discoverable but not safely writable yet"
    if row_column_sync != TranslationStatus.LIVE:
        return "row/column policy translation is discovery-only until semantics are reviewed"
    return "shortcut target is not enabled for writes"
