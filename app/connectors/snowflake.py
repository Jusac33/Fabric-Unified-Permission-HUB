"""Snowflake connector — wraps policy-weaver Snowflake plugin."""
from __future__ import annotations
from typing import List, Any
from .base import (
    SourceConnector, SourceType, Principal, DataObject, Grant, ConnectionResult,
)
from .databricks import _grants_from_policy_export


class SnowflakeConnector(SourceConnector):
    source_type = SourceType.SNOWFLAKE

    @classmethod
    def from_yaml(cls, config_path: str) -> "SnowflakeConnector":
        from policyweaver.plugins.snowflake.model import SnowflakeSourceMap
        inst = cls(config_path)
        inst._raw_config = SnowflakeSourceMap.from_yaml(config_path)
        return inst

    def _client(self):
        if self._pw_client is None:
            from policyweaver.plugins.snowflake.client import SnowflakePolicyWeaver
            self._pw_client = SnowflakePolicyWeaver(self._raw_config)
        return self._pw_client

    def test_connection(self) -> ConnectionResult:
        try:
            self._client()
            return ConnectionResult(ok=True, message="Snowflake client constructed")
        except Exception as e:
            return ConnectionResult(ok=False, message=str(e))

    def list_principals(self) -> List[Principal]:
        c = self._client()
        out: list[Principal] = []
        for u in (getattr(c, "users", None) or []):
            out.append(Principal(
                id=str(getattr(u, "name", "")),
                name=str(getattr(u, "name", "")),
                kind="user",
                email=getattr(u, "email", None),
                upn=getattr(u, "login_name", None),
            ))
        for r in (getattr(c, "roles", None) or []):
            out.append(Principal(
                id=str(getattr(r, "name", "")),
                name=str(getattr(r, "name", "")),
                kind="role",
            ))
        return out

    def list_objects(self) -> List[DataObject]:
        c = self._client()
        out: list[DataObject] = []
        for g in (getattr(c, "valid_grants", None) or []):
            out.append(DataObject(
                catalog=getattr(g, "table_catalog", None),
                schema=getattr(g, "table_schema", None),
                table=getattr(g, "name", None),
            ))
        # de-dupe
        seen: set[str] = set()
        uniq: list[DataObject] = []
        for o in out:
            if o.fqn and o.fqn not in seen:
                seen.add(o.fqn)
                uniq.append(o)
        return uniq

    def list_grants(self) -> List[Grant]:
        return _grants_from_policy_export(self.to_policy_export())

    def to_policy_export(self) -> Any:
        c = self._client()
        mapping = getattr(self._raw_config.fabric, "policy_mapping", "table_based")
        return c.map_policy(mapping)
