"""Base connector contract — every source plugin implements this."""
from __future__ import annotations
from abc import ABC, abstractmethod
from enum import Enum
from typing import Any, List, Optional
from pydantic import BaseModel, Field


class SourceType(str, Enum):
    DATABRICKS = "databricks"
    DATABRICKS_UC = "databricks_uc"
    SNOWFLAKE = "snowflake"
    SNOWFLAKE_HORIZON = "snowflake_horizon"
    AWS_LAKE_FORMATION_S3 = "aws_lake_formation_s3"
    FABRIC_ONELAKE = "fabric_onelake"
    FABRIC_SHORTCUT = "fabric_shortcut"
    DATAVERSE = "dataverse"


class Principal(BaseModel):
    """Normalized principal (user / group / service principal / role)."""
    id: str
    name: str
    kind: str  # user|group|role|sp
    email: Optional[str] = None
    upn: Optional[str] = None


class DataObject(BaseModel):
    """Normalized data object (catalog / schema / table)."""
    catalog: Optional[str] = None
    schema_: Optional[str] = Field(default=None, alias="schema")
    table: Optional[str] = None

    model_config = {"populate_by_name": True}

    @property
    def fqn(self) -> str:
        return ".".join(p for p in (self.catalog, self.schema_, self.table) if p)


class Grant(BaseModel):
    """Normalized grant: principal -> object -> permission."""
    principal: Principal
    object: DataObject
    permission: str        # SELECT, READ, etc.
    state: str = "GRANT"   # GRANT|DENY
    via_role: Optional[str] = None
    column_mask: Optional[str] = None
    row_filter: Optional[str] = None


class ConnectionResult(BaseModel):
    ok: bool
    message: str
    details: Optional[dict] = None


class SourceConnector(ABC):
    """Pluggable source connector abstraction."""

    source_type: SourceType
    config_path: str

    def __init__(self, config_path: str) -> None:
        self.config_path = config_path
        self._raw_config: Any = None
        self._pw_client: Any = None  # underlying policy-weaver client

    # --- factory ---
    @classmethod
    @abstractmethod
    def from_yaml(cls, config_path: str) -> "SourceConnector": ...

    # --- discovery ---
    @abstractmethod
    def test_connection(self) -> ConnectionResult: ...

    @abstractmethod
    def list_principals(self) -> List[Principal]: ...

    @abstractmethod
    def list_objects(self) -> List[DataObject]: ...

    @abstractmethod
    def list_grants(self) -> List[Grant]: ...

    # --- export to fabric (delegates to policy-weaver) ---
    @abstractmethod
    def to_policy_export(self) -> Any:
        """Return a policy-weaver PolicyExport / RolePolicyExport."""
        ...

    # --- shared helpers ---
    @property
    def display_name(self) -> str:
        return f"{self.source_type.value}:{self.config_path}"

    def summary(self) -> dict:
        try:
            principals = self.list_principals()
            objects = self.list_objects()
            grants = self.list_grants()
            return {
                "type": self.source_type.value,
                "principals": len(principals),
                "objects": len(objects),
                "grants": len(grants),
            }
        except Exception as e:  # surface to UI; do not crash dashboard
            return {"type": self.source_type.value, "error": str(e)}
