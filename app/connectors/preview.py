"""Preview-only connectors for source authorities without safe writers yet."""
from __future__ import annotations

from typing import Any, List

import yaml

from .base import (
    ConnectionResult,
    DataObject,
    Grant,
    Principal,
    SourceConnector,
    SourceType,
)


class PreviewOnlyConnector(SourceConnector):
    source_type = SourceType.FABRIC_SHORTCUT

    @classmethod
    def from_yaml(cls, config_path: str) -> "PreviewOnlyConnector":
        inst = cls(config_path)
        with open(config_path, "r", encoding="utf-8") as fh:
            inst._raw_config = yaml.safe_load(fh) or {}
        return inst

    def test_connection(self) -> ConnectionResult:
        return ConnectionResult(
            ok=True,
            message=(
                f"{self.source_type.value} is registered for read-only preview; "
                "permission writes are disabled"
            ),
            details={"preview_only": True},
        )

    def list_principals(self) -> List[Principal]:
        return []

    def list_objects(self) -> List[DataObject]:
        return []

    def list_grants(self) -> List[Grant]:
        return []

    def to_policy_export(self) -> Any:
        return {
            "source_type": self.source_type.value,
            "preview_only": True,
            "config_path": self.config_path,
        }


class AwsLakeFormationS3Connector(PreviewOnlyConnector):
    source_type = SourceType.AWS_LAKE_FORMATION_S3


class FabricOneLakeConnector(PreviewOnlyConnector):
    source_type = SourceType.FABRIC_ONELAKE


class FabricShortcutConnector(PreviewOnlyConnector):
    source_type = SourceType.FABRIC_SHORTCUT
