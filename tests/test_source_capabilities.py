from __future__ import annotations

from app.services.source_capabilities import (
    SourcePlatform,
    TranslationStatus,
    classify_shortcut_target,
    get_translation,
    shortcut_translation_plan,
)


def test_uc_to_fabric_translation_is_live_and_bidirectional() -> None:
    capability = get_translation(SourcePlatform.DATABRICKS_UC, SourcePlatform.FABRIC_ONELAKE)

    assert capability is not None
    assert capability.grant_sync == TranslationStatus.LIVE
    assert capability.row_column_sync == TranslationStatus.LIVE
    assert capability.bidirectional is True


def test_s3_shortcut_classifies_as_aws_source_authority() -> None:
    shortcut = {
        "name": "sales-s3",
        "path": "Tables/sales",
        "target": {
            "amazonS3": {
                "bucketName": "company-sales",
                "subpath": "/curated",
                "region": "us-east-1",
                "connectionId": "abc",
            }
        },
    }

    target = classify_shortcut_target(shortcut)
    plan = shortcut_translation_plan(shortcut)

    assert target.platform == SourcePlatform.AWS_LAKE_FORMATION_S3
    assert target.authority is True
    assert target.location == "company-sales/curated"
    assert plan["grant_sync"] == TranslationStatus.PLANNED
    assert plan["bidirectional"] is True


def test_unknown_shortcut_is_bridge_only_planned() -> None:
    shortcut = {
        "name": "mystery",
        "path": "Files/x",
        "target": {"unknownTarget": {"path": "/x"}},
    }

    target = classify_shortcut_target(shortcut)
    plan = shortcut_translation_plan(shortcut)

    assert target.platform == SourcePlatform.FABRIC_SHORTCUT
    assert target.authority is False
    assert plan["grant_sync"] == TranslationStatus.PLANNED
    assert plan["bidirectional"] is False


def test_snowflake_shortcut_target_is_planned_authority() -> None:
    shortcut = {
        "name": "snowflake-preview",
        "path": "Tables/customer",
        "target": {"snowflake": {"path": "DB.SCHEMA.CUSTOMER"}},
    }

    target = classify_shortcut_target(shortcut)
    plan = shortcut_translation_plan(shortcut)

    assert target.platform == SourcePlatform.SNOWFLAKE_HORIZON
    assert target.authority is True
    assert plan["grant_sync"] == TranslationStatus.PLANNED
    assert plan["bidirectional"] is True
