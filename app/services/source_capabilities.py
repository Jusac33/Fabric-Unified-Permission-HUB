"""OneSync-style source capability and shortcut translation registry.

This module is intentionally declarative: it tells the app which platforms can
be treated as authorities for permissions, which platforms are only Fabric
shortcut targets, and which translations are safe to apply today.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable


class SourcePlatform(str, Enum):
    FABRIC_ONELAKE = "fabric_onelake"
    FABRIC_SHORTCUT = "fabric_shortcut"
    DATABRICKS_UC = "databricks_uc"
    SNOWFLAKE = "snowflake"
    SNOWFLAKE_HORIZON = "snowflake_horizon"
    AWS_LAKE_FORMATION_S3 = "aws_lake_formation_s3"


class TranslationStatus(str, Enum):
    LIVE = "live"
    DISCOVERY_ONLY = "discovery_only"
    PLANNED = "planned"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True)
class SourceCapability:
    platform: SourcePlatform
    display_name: str
    authority: bool
    supported_scopes: tuple[str, ...]
    row_column_security: TranslationStatus
    connector_status: TranslationStatus
    notes: str


@dataclass(frozen=True)
class TranslationCapability:
    source: SourcePlatform
    target: SourcePlatform
    grant_sync: TranslationStatus
    row_column_sync: TranslationStatus
    bidirectional: bool
    notes: str


@dataclass(frozen=True)
class ShortcutTarget:
    target_type: str
    platform: SourcePlatform
    authority: bool
    location: str = ""
    connection_id: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


SOURCE_CAPABILITIES: tuple[SourceCapability, ...] = (
    SourceCapability(
        platform=SourcePlatform.FABRIC_ONELAKE,
        display_name="Microsoft Fabric OneLake",
        authority=True,
        supported_scopes=("workspace", "item", "path", "table", "row_constraint", "column_constraint"),
        row_column_security=TranslationStatus.LIVE,
        connector_status=TranslationStatus.LIVE,
        notes="Direct Fabric REST support for workspace roles, OneLake DAS roles, and DAS constraints.",
    ),
    SourceCapability(
        platform=SourcePlatform.FABRIC_SHORTCUT,
        display_name="Fabric OneLake shortcut",
        authority=False,
        supported_scopes=("shortcut_path", "target_reference"),
        row_column_security=TranslationStatus.UNSUPPORTED,
        connector_status=TranslationStatus.LIVE,
        notes="A shortcut is a bridge to another authority; permissions must be synced with the target system too.",
    ),
    SourceCapability(
        platform=SourcePlatform.DATABRICKS_UC,
        display_name="Databricks Unity Catalog",
        authority=True,
        supported_scopes=("catalog", "schema", "table", "row_filter", "column_mask"),
        row_column_security=TranslationStatus.LIVE,
        connector_status=TranslationStatus.LIVE,
        notes="Direct REST support for UC grants plus table metadata for row filters and column masks.",
    ),
    SourceCapability(
        platform=SourcePlatform.SNOWFLAKE_HORIZON,
        display_name="Snowflake Horizon/Catalog",
        authority=True,
        supported_scopes=("database", "schema", "table", "view", "row_access_policy", "masking_policy"),
        row_column_security=TranslationStatus.PLANNED,
        connector_status=TranslationStatus.PLANNED,
        notes="Existing legacy connector is PolicyWeaver-backed; direct Snowflake discovery/writer is still needed.",
    ),
    SourceCapability(
        platform=SourcePlatform.AWS_LAKE_FORMATION_S3,
        display_name="AWS Lake Formation / S3",
        authority=True,
        supported_scopes=("catalog_database", "table", "column", "lf_tag", "s3_bucket", "s3_prefix"),
        row_column_security=TranslationStatus.PLANNED,
        connector_status=TranslationStatus.PLANNED,
        notes="Needed for Amazon S3/S3-compatible shortcuts so source-side LF/IAM access matches OneLake DAS.",
    ),
)


TRANSLATION_CAPABILITIES: tuple[TranslationCapability, ...] = (
    TranslationCapability(
        SourcePlatform.DATABRICKS_UC,
        SourcePlatform.FABRIC_ONELAKE,
        TranslationStatus.LIVE,
        TranslationStatus.LIVE,
        bidirectional=True,
        notes="UC grants and RLS/CLS translate bidirectionally with Fabric roles/DAS where semantics map cleanly; unsupported rows are skipped safely.",
    ),
    TranslationCapability(
        SourcePlatform.SNOWFLAKE_HORIZON,
        SourcePlatform.FABRIC_ONELAKE,
        TranslationStatus.PLANNED,
        TranslationStatus.PLANNED,
        bidirectional=True,
        notes="Requires direct Snowflake grants, masking policies, and row access policy discovery/writers.",
    ),
    TranslationCapability(
        SourcePlatform.AWS_LAKE_FORMATION_S3,
        SourcePlatform.FABRIC_ONELAKE,
        TranslationStatus.PLANNED,
        TranslationStatus.PLANNED,
        bidirectional=True,
        notes="Required for S3 shortcuts; OneLake DAS must be paired with Lake Formation/IAM source permissions.",
    ),
)


_SHORTCUT_TARGETS: dict[str, tuple[str, SourcePlatform]] = {
    "amazonS3": ("Amazon S3", SourcePlatform.AWS_LAKE_FORMATION_S3),
    "s3Compatible": ("S3-compatible storage", SourcePlatform.AWS_LAKE_FORMATION_S3),
    "adlsGen2": ("ADLS Gen2", SourcePlatform.FABRIC_ONELAKE),
    "azureBlobStorage": ("Azure Blob Storage", SourcePlatform.FABRIC_ONELAKE),
    "googleCloudStorage": ("Google Cloud Storage", SourcePlatform.FABRIC_SHORTCUT),
    "snowflake": ("Snowflake", SourcePlatform.SNOWFLAKE_HORIZON),
    "oneLake": ("OneLake", SourcePlatform.FABRIC_ONELAKE),
}


def list_source_capabilities() -> list[SourceCapability]:
    return list(SOURCE_CAPABILITIES)


def list_translation_capabilities() -> list[TranslationCapability]:
    return list(TRANSLATION_CAPABILITIES)


def get_translation(source: SourcePlatform | str, target: SourcePlatform | str) -> TranslationCapability | None:
    src = SourcePlatform(source)
    dst = SourcePlatform(target)
    for capability in TRANSLATION_CAPABILITIES:
        if capability.source == src and capability.target == dst:
            return capability
        if capability.bidirectional and capability.source == dst and capability.target == src:
            return capability
    return None


def classify_shortcut_target(shortcut: dict[str, Any]) -> ShortcutTarget:
    target = shortcut.get("target") or {}
    if not isinstance(target, dict):
        target = {}
    for key, (display, platform) in _SHORTCUT_TARGETS.items():
        value = target.get(key)
        if not isinstance(value, dict):
            continue
        location = _target_location(key, value)
        return ShortcutTarget(
            target_type=display,
            platform=platform,
            authority=platform != SourcePlatform.FABRIC_SHORTCUT,
            location=location,
            connection_id=str(value.get("connectionId") or ""),
            raw=value,
        )
    return ShortcutTarget(
        target_type="Unknown",
        platform=SourcePlatform.FABRIC_SHORTCUT,
        authority=False,
        raw=target,
    )


def shortcut_translation_plan(shortcut: dict[str, Any]) -> dict[str, Any]:
    target = classify_shortcut_target(shortcut)
    translation = get_translation(target.platform, SourcePlatform.FABRIC_ONELAKE)
    if translation:
        grant_sync = translation.grant_sync
        row_column_sync = translation.row_column_sync
        notes = translation.notes
    elif target.platform == SourcePlatform.FABRIC_ONELAKE:
        grant_sync = TranslationStatus.LIVE
        row_column_sync = TranslationStatus.DISCOVERY_ONLY
        notes = "Shortcut target is already Azure/OneLake-side; Fabric DAS is the primary control plane."
    else:
        grant_sync = TranslationStatus.PLANNED
        row_column_sync = TranslationStatus.PLANNED
        notes = "Target authority connector is not implemented yet."
    return {
        "shortcut_name": shortcut.get("name") or "",
        "shortcut_path": shortcut.get("path") or "",
        "target": target,
        "fabric_side": SourcePlatform.FABRIC_ONELAKE,
        "grant_sync": grant_sync,
        "row_column_sync": row_column_sync,
        "bidirectional": bool(translation and translation.bidirectional),
        "notes": notes,
    }


def summarize_shortcuts(shortcuts: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return [shortcut_translation_plan(shortcut) for shortcut in shortcuts]


def _target_location(key: str, value: dict[str, Any]) -> str:
    if key in {"amazonS3", "s3Compatible", "googleCloudStorage"}:
        bucket = value.get("bucketName") or ""
        subpath = value.get("subpath") or ""
        return f"{bucket}{subpath}"
    if key in {"adlsGen2", "azureBlobStorage"}:
        location = value.get("location") or ""
        subpath = value.get("subpath") or ""
        return f"{location}{subpath}"
    if key == "oneLake":
        workspace_id = value.get("workspaceId") or ""
        item_id = value.get("itemId") or ""
        path = value.get("path") or ""
        return "/".join(part for part in (workspace_id, item_id, path) if part)
    return str(value.get("path") or value.get("url") or "")
