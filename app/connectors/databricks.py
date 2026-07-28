"""Databricks Unity Catalog connector — wraps policy-weaver UC plugin."""
from __future__ import annotations
from typing import List, Any
from .base import (
    SourceConnector, SourceType, Principal, DataObject, Grant, ConnectionResult,
)


class DatabricksConnector(SourceConnector):
    source_type = SourceType.DATABRICKS

    @classmethod
    def from_yaml(cls, config_path: str) -> "DatabricksConnector":
        from policyweaver.plugins.databricks.model import DatabricksSourceMap
        inst = cls(config_path)
        inst._raw_config = DatabricksSourceMap.from_yaml(config_path)
        return inst

    def _client(self):
        if self._pw_client is None:
            from policyweaver.plugins.databricks.client import DatabricksPolicyWeaver
            self._pw_client = DatabricksPolicyWeaver(self._raw_config)
        return self._pw_client

    def test_connection(self) -> ConnectionResult:
        try:
            c = self._client()
            # Touch the API: list catalogs
            getattr(c, "api", None) and getattr(c.api, "list_catalogs", lambda: None)()
            return ConnectionResult(ok=True, message="Databricks reachable")
        except Exception as e:
            return ConnectionResult(ok=False, message=str(e))

    def list_principals(self) -> List[Principal]:
        c = self._client()
        out: list[Principal] = []
        users = getattr(c, "users", None) or []
        for u in users:
            out.append(Principal(
                id=str(getattr(u, "id", "") or getattr(u, "user_name", "")),
                name=str(getattr(u, "user_name", "") or getattr(u, "display_name", "")),
                kind="user",
                email=getattr(u, "user_name", None),
            ))
        groups = getattr(c, "groups", None) or []
        for g in groups:
            out.append(Principal(
                id=str(getattr(g, "id", "") or getattr(g, "display_name", "")),
                name=str(getattr(g, "display_name", "")),
                kind="group",
            ))
        return out

    def list_objects(self) -> List[DataObject]:
        c = self._client()
        out: list[DataObject] = []
        catalogs = getattr(c, "catalogs", None) or []
        for cat in catalogs:
            cname = getattr(cat, "name", None)
            schemas = getattr(cat, "schemas", None) or []
            if not schemas:
                out.append(DataObject(catalog=cname))
                continue
            for sch in schemas:
                sname = getattr(sch, "name", None)
                tables = getattr(sch, "tables", None) or []
                if not tables:
                    out.append(DataObject(catalog=cname, schema=sname))
                    continue
                for t in tables:
                    out.append(DataObject(
                        catalog=cname, schema=sname,
                        table=getattr(t, "name", None),
                    ))
        return out

    def list_grants(self) -> List[Grant]:
        export = self.to_policy_export()
        return _grants_from_policy_export(export)

    def to_policy_export(self) -> Any:
        c = self._client()
        mapping = getattr(self._raw_config.fabric, "policy_mapping", "table_based")
        return c.map_policy(mapping)


def _grants_from_policy_export(export: Any) -> List[Grant]:
    grants: list[Grant] = []
    if not export or not getattr(export, "policies", None):
        return grants
    for p in export.policies:
        cat = getattr(p, "catalog", None)
        sch = getattr(p, "catalog_schema", None)
        tbl = getattr(p, "table", None)
        obj = DataObject(catalog=cat, schema=sch, table=tbl)
        for perm in getattr(p, "permissions", None) or []:
            for o in getattr(perm, "objects", None) or []:
                grants.append(Grant(
                    principal=Principal(
                        id=str(getattr(o, "lookup_id", "") or getattr(o, "id", "")),
                        name=str(getattr(o, "lookup_id", "") or getattr(o, "name", "")),
                        kind=str(getattr(o, "type", "user")).lower(),
                    ),
                    object=obj,
                    permission=str(getattr(perm, "name", "SELECT")),
                    state=str(getattr(perm, "state", "GRANT")),
                ))
    return grants
