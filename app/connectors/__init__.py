"""Pluggable source connectors. Wraps policy-weaver plugins with a unified API."""
from __future__ import annotations
from typing import Dict, Type
from .base import SourceConnector, SourceType
from .databricks import DatabricksConnector
from .snowflake import SnowflakeConnector
from .dataverse import DataverseConnector
from .preview import (
    AwsLakeFormationS3Connector,
    FabricOneLakeConnector,
    FabricShortcutConnector,
)

REGISTRY: Dict[SourceType, Type[SourceConnector]] = {
    SourceType.DATABRICKS: DatabricksConnector,
    SourceType.DATABRICKS_UC: DatabricksConnector,
    SourceType.SNOWFLAKE: SnowflakeConnector,
    SourceType.SNOWFLAKE_HORIZON: SnowflakeConnector,
    SourceType.DATAVERSE: DataverseConnector,
    SourceType.AWS_LAKE_FORMATION_S3: AwsLakeFormationS3Connector,
    SourceType.FABRIC_ONELAKE: FabricOneLakeConnector,
    SourceType.FABRIC_SHORTCUT: FabricShortcutConnector,
}


def get_connector(source_type: str | SourceType, config_path: str) -> SourceConnector:
    st = SourceType(source_type) if isinstance(source_type, str) else source_type
    cls = REGISTRY.get(st)
    if not cls:
        raise ValueError(f"Unsupported source type: {source_type}")
    return cls.from_yaml(config_path)


__all__ = ["SourceConnector", "SourceType", "REGISTRY", "get_connector"]
