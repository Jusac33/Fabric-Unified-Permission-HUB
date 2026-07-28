"""Dataverse connector — wraps policy-weaver Dataverse plugin (Beta upstream)."""
from __future__ import annotations
from typing import List, Any
from .base import (
    SourceConnector, SourceType, Principal, DataObject, Grant, ConnectionResult,
)
from .databricks import _grants_from_policy_export


class DataverseConnector(SourceConnector):
    source_type = SourceType.DATAVERSE

    @classmethod
    def from_yaml(cls, config_path: str) -> "DataverseConnector":
        from policyweaver.plugins.dataverse.model import DataverseSourceMap
        inst = cls(config_path)
        inst._raw_config = DataverseSourceMap.from_yaml(config_path)
        return inst

    def _client(self):
        if self._pw_client is None:
            from policyweaver.plugins.dataverse.client import DataversePolicyWeaver
            self._pw_client = DataversePolicyWeaver(self._raw_config)
        return self._pw_client

    def test_connection(self) -> ConnectionResult:
        try:
            self._client()
            return ConnectionResult(ok=True, message="Dataverse client constructed")
        except Exception as e:
            return ConnectionResult(ok=False, message=str(e))

    def list_principals(self) -> List[Principal]:
        c = self._client()
        out: list[Principal] = []
        for u in (getattr(c, "users", None) or []):
            out.append(Principal(
                id=str(getattr(u, "azure_object_id", "") or getattr(u, "id", "")),
                name=str(getattr(u, "full_name", "") or getattr(u, "domain_name", "")),
                kind="user",
                upn=getattr(u, "domain_name", None),
            ))
        for t in (getattr(c, "teams", None) or []):
            out.append(Principal(
                id=str(getattr(t, "id", "") or getattr(t, "name", "")),
                name=str(getattr(t, "name", "")),
                kind="group",
            ))
        for r in (getattr(c, "security_roles", None) or []):
            out.append(Principal(
                id=str(getattr(r, "id", "") or getattr(r, "name", "")),
                name=str(getattr(r, "name", "")),
                kind="role",
            ))
        return out

    def list_objects(self) -> List[DataObject]:
        c = self._client()
        out: list[DataObject] = []
        for r in (getattr(c, "security_roles", None) or []):
            for tp in (getattr(r, "table_privileges", None) or []):
                out.append(DataObject(
                    catalog=getattr(self._raw_config.source, "name", None),
                    schema="dbo",
                    table=getattr(tp, "table_name", None),
                ))
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
        mapping = getattr(self._raw_config.fabric, "policy_mapping", "role_based")
        return c.map_policy(mapping)
