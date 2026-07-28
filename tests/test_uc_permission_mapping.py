from __future__ import annotations

import httpx
import pytest

from app.services import pair_apply, pair_diff
from app.services.databricks_rest import DatabricksUCClient
from app.services.pair_apply import (
    _dar_role_matches,
    _uc_grant_operations,
    _uc_privileges_for_scope,
    _uc_target_from_scope,
    apply_rows,
)
from app.services.pair_diff import (
    DiffRow,
    TABLE_POLICY_PRINCIPAL,
    _scopes_from_dar_paths,
    collect_dbx_rows,
    collect_fabric_rows,
)


def test_uc_target_from_workspace_scope_maps_to_catalog() -> None:
    assert _uc_target_from_scope("workspace:43fa6fa9-d863-e811-a838-000d3a309c3d", "main") == (
        "catalog",
        "main",
        "uc_catalog_grant",
    )


def test_uc_target_from_schema_scope_preserves_full_name() -> None:
    assert _uc_target_from_scope("schema:main.gold", "main") == (
        "schema",
        "main.gold",
        "uc_schema_grant",
    )


def test_uc_target_from_table_scope_preserves_three_level_name() -> None:
    assert _uc_target_from_scope("table:main.gold.orders", "main") == (
        "table",
        "main.gold.orders",
        "uc_table_grant",
    )


def test_uc_privileges_are_scope_specific() -> None:
    assert _uc_privileges_for_scope("DATA_READ", "catalog") == ["USE_CATALOG", "SELECT"]
    assert _uc_privileges_for_scope("DATA_READ", "schema") == ["USE_SCHEMA", "SELECT"]
    assert _uc_privileges_for_scope("DATA_READ", "table") == ["SELECT"]


def test_uc_grant_operations_add_parent_privileges_for_table() -> None:
    assert _uc_grant_operations("table", "main.gold.orders", ["SELECT"]) == [
        ("catalog", "main", ["USE_CATALOG"]),
        ("schema", "main.gold", ["USE_SCHEMA"]),
        ("table", "main.gold.orders", ["SELECT"]),
    ]


def test_scopes_from_dar_paths_handles_tables_prefix() -> None:
    assert _scopes_from_dar_paths("main", ["/Tables/gold/orders"]) == [
        "table:main.gold.orders"
    ]


def test_scopes_from_dar_paths_handles_catalog_wildcard() -> None:
    assert _scopes_from_dar_paths("main", ["*"]) == ["catalog:main"]


def test_fabric_to_dbx_dry_run_uses_table_scope() -> None:
    report = apply_rows(
        {
            "dbx_workspace_url": "https://adb.example.azuredatabricks.net",
            "uc_catalog": "main",
            "fabric_workspace_id": "43fa6fa9-d863-e811-a838-000d3a309c3d",
        },
        [
            DiffRow(
                principal_key="user@example.com",
                principal_display="user@example.com",
                principal_type="User",
                securable_scope="table:main.gold.orders",
                access_class="DATA_READ",
                on_fabric=True,
            )
        ],
        "fabric_to_dbx",
        dry_run=True,
    )

    action = report.actions[0]

    assert action.ok is True
    assert action.layer == "uc_table_grant"
    assert action.target_action == (
        "GRANT USE_CATALOG on catalog main; "
        "GRANT USE_SCHEMA on schema main.gold; "
        "GRANT SELECT on table main.gold.orders"
    )


def test_dbx_to_fabric_schema_scope_is_skipped() -> None:
    report = apply_rows(
        {
            "dbx_workspace_url": "https://adb-123.4.azuredatabricks.net",
            "uc_catalog": "main",
            "fabric_workspace_id": "43fa6fa9-d863-e811-a838-000d3a309c3d",
        },
        [
            DiffRow(
                principal_key="user@example.com",
                principal_display="user@example.com",
                principal_type="User",
                securable_scope="schema:main.gold",
                access_class="DATA_READ",
                on_dbx=True,
            )
        ],
        "dbx_to_fabric",
        dry_run=False,
    )

    action = report.actions[0]

    assert action.ok is True
    assert action.skipped is True
    assert action.layer == "unsupported_schema"


def test_dbx_to_fabric_table_without_exact_mirror_is_skipped(monkeypatch) -> None:
    monkeypatch.setattr(
        pair_apply.fabric_rest,
        "list_items",
        lambda _workspace_id: [
            {
                "id": "22222222-2222-2222-2222-222222222222",
                "type": "MirroredAzureDatabricksCatalog",
                "displayName": "other_catalog",
            }
        ],
    )

    report = apply_rows(
        {
            "dbx_workspace_url": "https://adb-123.4.azuredatabricks.net",
            "uc_catalog": "main",
            "fabric_workspace_id": "43fa6fa9-d863-e811-a838-000d3a309c3d",
        },
        [
            DiffRow(
                principal_key="user@example.com",
                principal_display="user@example.com",
                principal_type="User",
                securable_scope="table:main.gold.orders",
                access_class="DATA_READ",
                on_dbx=True,
            )
        ],
        "dbx_to_fabric",
        dry_run=False,
    )

    action = report.actions[0]

    assert action.ok is True
    assert action.skipped is True
    assert "refusing workspace-role fallback" in action.message


def test_dbx_to_fabric_table_requires_mirrored_catalog_item_type(monkeypatch) -> None:
    monkeypatch.setattr(
        pair_apply.fabric_rest,
        "list_items",
        lambda _workspace_id: [
            {
                "id": "33333333-3333-3333-3333-333333333333",
                "type": "Lakehouse",
                "displayName": "main",
            }
        ],
    )

    report = apply_rows(
        {
            "dbx_workspace_url": "https://adb-123.4.azuredatabricks.net",
            "uc_catalog": "main",
            "fabric_workspace_id": "43fa6fa9-d863-e811-a838-000d3a309c3d",
        },
        [
            DiffRow(
                principal_key="user@example.com",
                principal_display="user@example.com",
                principal_type="User",
                securable_scope="table:main.gold.orders",
                access_class="DATA_READ",
                on_dbx=True,
            )
        ],
        "dbx_to_fabric",
        dry_run=False,
    )

    action = report.actions[0]

    assert action.ok is True
    assert action.skipped is True
    assert "no exact Fabric mirrored catalog item match" in action.message


def test_dbx_to_fabric_table_item_lookup_failure_is_failed(monkeypatch) -> None:
    def _raise_items(_workspace_id):
        request = httpx.Request("GET", "https://api.fabric.microsoft.com/v1/test")
        response = httpx.Response(403, request=request)
        raise httpx.HTTPStatusError("forbidden", request=request, response=response)

    monkeypatch.setattr(pair_apply.fabric_rest, "list_items", _raise_items)

    report = apply_rows(
        {
            "dbx_workspace_url": "https://adb-123.4.azuredatabricks.net",
            "uc_catalog": "main",
            "fabric_workspace_id": "43fa6fa9-d863-e811-a838-000d3a309c3d",
        },
        [
            DiffRow(
                principal_key="user@example.com",
                principal_display="user@example.com",
                principal_type="User",
                securable_scope="table:main.gold.orders",
                access_class="DATA_READ",
                on_dbx=True,
            )
        ],
        "dbx_to_fabric",
        dry_run=False,
    )

    action = report.actions[0]

    assert action.ok is False
    assert action.skipped is False
    assert "failed to verify Fabric mirrored catalog item" in action.message


def test_dbx_to_fabric_write_on_mirrored_table_is_skipped(monkeypatch) -> None:
    monkeypatch.setattr(
        pair_apply.fabric_rest,
        "list_items",
        lambda _workspace_id: [
            {
                "id": "11111111-1111-1111-1111-111111111111",
                "type": "MirroredAzureDatabricksCatalog",
                "displayName": "main",
            }
        ],
    )

    report = apply_rows(
        {
            "dbx_workspace_url": "https://adb-123.4.azuredatabricks.net",
            "uc_catalog": "main",
            "fabric_workspace_id": "43fa6fa9-d863-e811-a838-000d3a309c3d",
        },
        [
            DiffRow(
                principal_key="user@example.com",
                principal_display="user@example.com",
                principal_type="User",
                securable_scope="table:main.gold.orders",
                access_class="DATA_WRITE",
                on_dbx=True,
            )
        ],
        "dbx_to_fabric",
        dry_run=False,
    )

    action = report.actions[0]

    assert action.ok is True
    assert action.skipped is True
    assert "read-only" in action.message


def test_abac_policy_is_skipped_on_apply(monkeypatch) -> None:
    """ABAC tag-driven policies must never be materialized onto Fabric."""

    def _boom(*_args, **_kwargs):
        raise AssertionError("ABAC rows must not reach the Fabric apply path")

    monkeypatch.setattr(pair_apply, "_find_mirrored_catalog_item", _boom)
    monkeypatch.setattr(pair_apply, "_fabric_constraint_translation", _boom)

    report = apply_rows(
        {
            "dbx_workspace_url": "https://adb.example.azuredatabricks.net",
            "uc_catalog": "main",
            "fabric_workspace_id": "43fa6fa9-d863-e811-a838-000d3a309c3d",
        },
        [
            DiffRow(
                principal_key=TABLE_POLICY_PRINCIPAL,
                principal_display="Table policy",
                principal_type="Policy",
                securable_scope="table:main.gold.orders",
                access_class="DATA_READ",
                on_dbx=True,
                constraint_kind="column_mask",
                constraint_key="abac1",
                constraint_details={
                    "source": "databricks_abac",
                    "policy_name": "mask_ssn",
                    "function": "main.gov.mask_ssn",
                    "on_column": "ssn",
                },
            )
        ],
        "dbx_to_fabric",
        dry_run=False,
    )

    action = report.actions[0]
    assert action.ok is True
    assert action.skipped is True
    assert "ABAC" in action.message


def test_dbx_to_fabric_row_filter_applies_fabric_das_constraint(monkeypatch) -> None:
    captured = {}

    class FakeClient:
        def __init__(self, _url):
            pass

        def get_function(self, full_name):
            assert full_name == "main.gold.allow_us"
            return {
                "data_type": "BOOLEAN",
                "routine_definition": "region = 'US'",
                "input_params": {
                    "parameters": [
                        {"name": "region", "parameter_type": "PARAM"},
                    ]
                },
            }

    monkeypatch.setattr(pair_apply, "DatabricksUCClient", FakeClient)
    monkeypatch.setattr(pair_apply, "get_fabric_token", lambda: "token")
    monkeypatch.setattr(
        pair_apply,
        "_current_entra_member",
        lambda: {
            "objectId": "00000000-0000-0000-0000-000000000001",
            "tenantId": "tenant",
            "objectType": "User",
        },
    )
    monkeypatch.setattr(
        pair_apply.fabric_rest,
        "list_items",
        lambda _workspace_id: [
            {
                "id": "11111111-1111-1111-1111-111111111111",
                "type": "MirroredAzureDatabricksCatalog",
                "displayName": "main",
            }
        ],
    )

    def fake_get(url, **_kwargs):
        return httpx.Response(200, json={"value": []}, request=httpx.Request("GET", url))

    def fake_put(url, **kwargs):
        captured["payload"] = kwargs["json"]
        return httpx.Response(200, json={}, request=httpx.Request("PUT", url))

    monkeypatch.setattr(pair_apply.httpx, "get", fake_get)
    monkeypatch.setattr(pair_apply.httpx, "put", fake_put)

    report = apply_rows(
        {
            "dbx_workspace_url": "https://adb.example.azuredatabricks.net",
            "uc_catalog": "main",
            "fabric_workspace_id": "43fa6fa9-d863-e811-a838-000d3a309c3d",
        },
        [
            DiffRow(
                principal_key=TABLE_POLICY_PRINCIPAL,
                principal_display="Table policy",
                principal_type="Policy",
                securable_scope="table:main.gold.orders",
                access_class="DATA_READ",
                on_dbx=True,
                constraint_kind="row_filter",
                constraint_key="abc",
                constraint_details={
                    "function": "main.gold.allow_us",
                    "input_columns": ["region"],
                },
            )
        ],
        "dbx_to_fabric",
        dry_run=False,
    )

    action = report.actions[0]
    role = captured["payload"]["value"][0]
    constraints = role["decisionRules"][0]["constraints"]["rows"][0]

    assert action.ok is True
    assert action.skipped is False
    assert constraints == {
        "tablePath": "/Tables/gold/orders",
        "value": "select * from gold.orders where region = 'US'",
        "type": "Fabric",
    }


def test_dbx_to_fabric_column_mask_applies_column_visibility_constraint(monkeypatch) -> None:
    captured = {}

    monkeypatch.setattr(pair_apply, "get_fabric_token", lambda: "token")
    monkeypatch.setattr(
        pair_apply,
        "_current_entra_member",
        lambda: {
            "objectId": "00000000-0000-0000-0000-000000000001",
            "tenantId": "tenant",
            "objectType": "User",
        },
    )
    monkeypatch.setattr(
        pair_apply.fabric_rest,
        "list_items",
        lambda _workspace_id: [
            {
                "id": "11111111-1111-1111-1111-111111111111",
                "type": "MirroredAzureDatabricksCatalog",
                "displayName": "main",
            }
        ],
    )

    def fake_get(url, **_kwargs):
        return httpx.Response(200, json={"value": []}, request=httpx.Request("GET", url))

    def fake_put(url, **kwargs):
        captured["payload"] = kwargs["json"]
        return httpx.Response(200, json={}, request=httpx.Request("PUT", url))

    monkeypatch.setattr(pair_apply.httpx, "get", fake_get)
    monkeypatch.setattr(pair_apply.httpx, "put", fake_put)

    report = apply_rows(
        {
            "dbx_workspace_url": "https://adb.example.azuredatabricks.net",
            "uc_catalog": "main",
            "fabric_workspace_id": "43fa6fa9-d863-e811-a838-000d3a309c3d",
        },
        [
            DiffRow(
                principal_key=TABLE_POLICY_PRINCIPAL,
                principal_display="Table policy",
                principal_type="Policy",
                securable_scope="table:main.gold.customers",
                access_class="DATA_READ",
                on_dbx=True,
                constraint_kind="column_mask",
                constraint_key="abc",
                constraint_details={
                    "table": "main.gold.customers",
                    "column": "email",
                    "table_columns": ["id", "email", "country"],
                },
            )
        ],
        "dbx_to_fabric",
        dry_run=False,
    )

    action = report.actions[0]
    role = captured["payload"]["value"][0]
    constraints = role["decisionRules"][0]["constraints"]["columns"][0]

    assert action.ok is True
    assert action.skipped is False
    assert constraints == {
        "tablePath": "/Tables/gold/customers",
        "columnNames": ["id", "country"],
        "columnEffect": "Permit",
        "columnAction": ["Read"],
    }


def test_dar_role_match_requires_same_scope_and_actions() -> None:
    role = {
        "decisionRules": [
            {
                "permission": [
                    {
                        "attributeName": "Path",
                        "attributeValueIncludedIn": ["/Tables/gold/orders"],
                    },
                    {
                        "attributeName": "Action",
                        "attributeValueIncludedIn": ["Read"],
                    },
                ]
            }
        ]
    }

    assert _dar_role_matches(role, "/Tables/gold/orders", ["Read"]) is True
    assert _dar_role_matches(role, "/Tables/gold/payroll", ["Read"]) is False


def test_dar_role_match_reads_split_permission_and_attributes() -> None:
    role = {
        "decisionRules": [
            {
                "attributes": [
                    {
                        "attributeName": "Path",
                        "attributeValueIncludedIn": ["/Tables/gold/orders"],
                    }
                ],
                "permission": [
                    {
                        "attributeName": "Action",
                        "attributeValueIncludedIn": ["Read"],
                    }
                ],
            }
        ]
    }

    assert _dar_role_matches(role, "/Tables/gold/orders", ["Read"]) is True


def test_fabric_to_dbx_apply_calls_scope_aware_uc_grant(monkeypatch) -> None:
    calls = []

    def _capture_grant(dbx_url, securable_type, full_name, principal, privileges):
        calls.append((dbx_url, securable_type, full_name, principal, privileges))
        return True, "granted"

    monkeypatch.setattr(pair_apply, "_grant_uc_privileges", _capture_grant)

    report = apply_rows(
        {
            "dbx_workspace_url": "https://adb.example.azuredatabricks.net",
            "uc_catalog": "main",
            "fabric_workspace_id": "43fa6fa9-d863-e811-a838-000d3a309c3d",
        },
        [
            DiffRow(
                principal_key="user@example.com",
                principal_display="user@example.com",
                principal_type="User",
                securable_scope="schema:main.gold",
                access_class="DATA_WRITE",
                on_fabric=True,
            )
        ],
        "fabric_to_dbx",
        dry_run=False,
    )

    assert report.actions[0].ok is True
    assert calls == [
        (
            "https://adb.example.azuredatabricks.net",
            "schema",
            "main.gold",
            "user@example.com",
            ["USE_SCHEMA", "MODIFY"],
        )
    ]


def test_fabric_to_dbx_row_constraint_applies_uc_row_filter(monkeypatch) -> None:
    calls = []

    class FakeClient:
        def __init__(self, _url):
            pass

        def get_table(self, full_name):
            assert full_name == "main.gold.customers"
            return {
                "columns": [
                    {"name": "id", "type_text": "bigint"},
                    {"name": "country", "type_text": "string"},
                ]
            }

        def ensure_sql_warehouse_running(self, warehouse_id):
            calls.append(("ensure", warehouse_id))

        def execute_sql(self, warehouse_id, statement):
            calls.append(("sql", warehouse_id, statement))
            return {}

    monkeypatch.setattr(pair_apply, "DatabricksUCClient", FakeClient)
    monkeypatch.setattr(pair_apply.settings, "DBX_WAREHOUSE_ID", "wh123")

    report = apply_rows(
        {
            "dbx_workspace_url": "https://adb.example.azuredatabricks.net",
            "uc_catalog": "main",
            "fabric_workspace_id": "43fa6fa9-d863-e811-a838-000d3a309c3d",
        },
        [
            DiffRow(
                principal_key=TABLE_POLICY_PRINCIPAL,
                principal_display="Table policy",
                principal_type="Policy",
                securable_scope="table:main.gold.customers",
                access_class="DATA_READ",
                on_fabric=True,
                constraint_kind="row_filter",
                constraint_key="abc",
                constraint_details={
                    "predicate": "select * from gold.customers where country = 'US'",
                    "source": "fabric",
                },
            )
        ],
        "fabric_to_dbx",
        dry_run=False,
    )

    sql_statements = [call[2] for call in calls if call[0] == "sql"]
    assert report.actions[0].ok is True
    assert calls[0] == ("ensure", "wh123")
    assert len(sql_statements) == 2
    assert "CREATE OR REPLACE FUNCTION main.gold.hub_fabric_rls_" in sql_statements[0]
    assert "RETURNS BOOLEAN RETURN country = 'US'" in sql_statements[0]
    assert "ALTER TABLE main.gold.customers SET ROW FILTER" in sql_statements[1]
    assert "ON (country)" in sql_statements[1]


def test_fabric_to_dbx_column_constraint_applies_uc_mask(monkeypatch) -> None:
    calls = []

    class FakeClient:
        def __init__(self, _url):
            pass

        def get_table(self, full_name):
            assert full_name == "main.gold.customers"
            return {
                "columns": [
                    {"name": "id", "type_text": "bigint"},
                    {"name": "name", "type_text": "string"},
                    {"name": "email", "type_text": "string"},
                    {"name": "country", "type_text": "string"},
                ]
            }

        def ensure_sql_warehouse_running(self, warehouse_id):
            calls.append(("ensure", warehouse_id))

        def execute_sql(self, warehouse_id, statement):
            calls.append(("sql", warehouse_id, statement))
            return {}

    monkeypatch.setattr(pair_apply, "DatabricksUCClient", FakeClient)
    monkeypatch.setattr(pair_apply.settings, "DBX_WAREHOUSE_ID", "wh123")

    report = apply_rows(
        {
            "dbx_workspace_url": "https://adb.example.azuredatabricks.net",
            "uc_catalog": "main",
            "fabric_workspace_id": "43fa6fa9-d863-e811-a838-000d3a309c3d",
        },
        [
            DiffRow(
                principal_key=TABLE_POLICY_PRINCIPAL,
                principal_display="Table policy",
                principal_type="Policy",
                securable_scope="table:main.gold.customers",
                access_class="DATA_READ",
                on_fabric=True,
                constraint_kind="column_mask",
                constraint_key="abc",
                constraint_details={
                    "columns": ["id", "name", "country"],
                    "column_effect": "Permit",
                    "column_action": ["Read"],
                    "source": "fabric",
                },
            )
        ],
        "fabric_to_dbx",
        dry_run=False,
    )

    sql_statements = [call[2] for call in calls if call[0] == "sql"]
    assert report.actions[0].ok is True
    assert calls[0] == ("ensure", "wh123")
    assert len(sql_statements) == 2
    assert "CREATE OR REPLACE FUNCTION main.gold.hub_fabric_mask_" in sql_statements[0]
    assert "RETURNS string RETURN '***MASKED***'" in sql_statements[0]
    assert "ALTER TABLE main.gold.customers ALTER COLUMN email SET MASK" in sql_statements[1]


def _patch_fabric_dar(monkeypatch, dars: list[dict]) -> None:
    monkeypatch.setattr(pair_diff.fabric_rest, "list_role_assignments", lambda _workspace_id: [])
    monkeypatch.setattr(
        pair_diff.fabric_rest,
        "list_items",
        lambda _workspace_id: [
            {
                "id": "11111111-1111-1111-1111-111111111111",
                "type": "MirroredAzureDatabricksCatalog",
                "displayName": "main",
            }
        ],
    )
    monkeypatch.setattr(
        pair_diff.fabric_rest,
        "list_data_access_policies",
        lambda _workspace_id, _item_id: dars,
    )


def test_collect_fabric_rows_reads_writer_dar_payload_shape(monkeypatch) -> None:
    principal_id = "00000000-0000-0000-0000-000000000001"
    _patch_fabric_dar(
        monkeypatch,
        [
            {
                "members": {
                    "microsoftEntraMembers": [
                        {"objectId": principal_id, "objectType": "User"}
                    ]
                },
                "decisionRules": [
                    {
                        "permission": [
                            {
                                "attributeName": "Path",
                                "attributeValueIncludedIn": ["/Tables/gold/orders"],
                            },
                            {
                                "attributeName": "Action",
                                "attributeValueIncludedIn": ["Read"],
                            },
                        ]
                    }
                ],
            }
        ],
    )

    rows = collect_fabric_rows(
        "43fa6fa9-d863-e811-a838-000d3a309c3d",
        "main",
    )

    assert {(row.principal_key, row.securable_scope, row.access_class) for row in rows} == {
        (principal_id, "table:main.gold.orders", "DATA_READ")
    }


def test_collect_fabric_rows_reads_das_row_and_column_constraints(monkeypatch) -> None:
    principal_id = "00000000-0000-0000-0000-000000000001"
    _patch_fabric_dar(
        monkeypatch,
        [
            {
                "members": {
                    "microsoftEntraMembers": [
                        {"objectId": principal_id, "objectType": "User"}
                    ]
                },
                "decisionRules": [
                    {
                        "permission": [
                            {
                                "attributeName": "Path",
                                "attributeValueIncludedIn": ["/Tables/gold/orders"],
                            },
                            {
                                "attributeName": "Action",
                                "attributeValueIncludedIn": ["Read"],
                            },
                        ],
                        "constraints": {
                            "rows": [
                                {
                                    "tablePath": "/Tables/gold/orders",
                                    "value": "select * from gold.orders where region = 'US'",
                                    "type": "Fabric",
                                }
                            ],
                            "columns": [
                                {
                                    "tablePath": "/Tables/gold/orders",
                                    "columnNames": ["id", "region"],
                                    "columnEffect": "Permit",
                                    "columnAction": ["Read"],
                                }
                            ],
                        },
                    }
                ],
            }
        ],
    )

    rows = collect_fabric_rows(
        "43fa6fa9-d863-e811-a838-000d3a309c3d",
        "main",
    )
    constraints = {
        (row.constraint_kind, row.securable_scope, row.constraint_details.get("predicate"))
        for row in rows
        if row.constraint_kind
    }

    assert (
        "row_filter",
        "table:main.gold.orders",
        "select * from gold.orders where region = 'US'",
    ) in constraints
    assert any(
        row.constraint_kind == "column_mask"
        and row.constraint_details["columns"] == ["id", "region"]
        for row in rows
    )
    assert sum(1 for row in rows if row.securable_scope == "table:main.gold.orders") == 3


def test_collect_dbx_rows_reads_uc_row_filter_and_column_mask(monkeypatch) -> None:
    class FakeClient:
        def __init__(self, _url):
            pass

        def get_grants(self, securable_type, full_name):
            if securable_type == "table" and full_name == "main.gold.orders":
                return [{"principal": "user@example.com", "privileges": ["SELECT"]}]
            return []

        def list_schemas(self, catalog):
            assert catalog == "main"
            return [{"name": "gold"}]

        def list_tables(self, catalog, schema):
            assert (catalog, schema) == ("main", "gold")
            return [{"name": "orders"}]

        def get_table(self, full_name):
            assert full_name == "main.gold.orders"
            return {
                "row_filters": [
                    {
                        "function_name": "main.gold.allow_us",
                        "input_column_names": ["region"],
                    }
                ],
                "columns": [
                    {
                        "name": "email",
                        "mask": {
                            "function_name": "main.gold.mask_email",
                            "input_column_names": ["email"],
                        },
                    }
                ],
            }

        def list_policies(self, securable_type, full_name, include_inherited=False):
            return []

    monkeypatch.setattr(pair_diff, "DatabricksUCClient", FakeClient)

    rows = collect_dbx_rows("https://adb-123.4.azuredatabricks.net", "main")
    constraint_rows = [row for row in rows if row.constraint_kind]
    assert {(row.constraint_kind, row.principal_key) for row in constraint_rows} == {
        ("row_filter", TABLE_POLICY_PRINCIPAL),
        ("column_mask", TABLE_POLICY_PRINCIPAL),
    }
    assert any(
        row.constraint_details["function"] == "main.gold.allow_us"
        and row.constraint_details["input_columns"] == ["region"]
        for row in constraint_rows
    )
    assert any(
        row.constraint_details.get("column") == "email"
        and row.constraint_details.get("function") == "main.gold.mask_email"
        for row in constraint_rows
    )


def test_collect_dbx_rows_surfaces_abac_policies(monkeypatch) -> None:
    class FakeClient:
        def __init__(self, _url):
            pass

        def get_grants(self, securable_type, full_name):
            return []

        def list_schemas(self, catalog):
            return [{"name": "gold"}]

        def list_tables(self, catalog, schema):
            return [{"name": "orders"}]

        def get_table(self, full_name):
            return {}

        def list_policies(self, securable_type, full_name, include_inherited=False):
            if securable_type == "catalog":
                return [
                    {
                        "id": "pol-row-1",
                        "name": "us_only",
                        "policy_type": "POLICY_TYPE_ROW_FILTER",
                        "on_securable_type": "CATALOG",
                        "on_securable_fullname": "main",
                        "row_filter": {"function_name": "main.gov.us_only"},
                        "to_principals": ["analysts"],
                        "when_condition": "hasTag('pii')",
                    }
                ]
            if securable_type == "table":
                return [
                    {
                        "id": "pol-mask-1",
                        "name": "mask_ssn",
                        "policy_type": "POLICY_TYPE_COLUMN_MASK",
                        "on_securable_type": "TABLE",
                        "on_securable_fullname": "main.gold.orders",
                        "column_mask": {
                            "function_name": "main.gov.mask_ssn",
                            "on_column": "ssn",
                        },
                        "to_principals": ["analysts"],
                        "when_condition": "hasTagValue('classification', 'sensitive')",
                    }
                ]
            return []

    monkeypatch.setattr(pair_diff, "DatabricksUCClient", FakeClient)

    rows = collect_dbx_rows("https://adb-123.4.azuredatabricks.net", "main")
    abac_rows = [
        row
        for row in rows
        if row.constraint_details.get("source") == "databricks_abac"
    ]

    assert {(row.constraint_kind, row.securable_scope) for row in abac_rows} == {
        ("row_filter", "catalog:main"),
        ("column_mask", "table:main.gold.orders"),
    }
    for row in abac_rows:
        assert row.principal_key == TABLE_POLICY_PRINCIPAL
        assert row.on_dbx is True
        assert row.has_row_col_diff is True
        assert row.constraint_details["when_condition"]
        # Tag-driven ABAC has no static Fabric equivalent — flagged, not faked.
        assert "no Fabric OneLake equivalent" in row.constraint_details["translation_error"]

    mask_row = next(r for r in abac_rows if r.constraint_kind == "column_mask")
    assert mask_row.constraint_details["on_column"] == "ssn"
    assert mask_row.constraint_details["function"] == "main.gov.mask_ssn"


def test_list_policies_returns_empty_when_abac_unsupported(monkeypatch) -> None:
    client = DatabricksUCClient.__new__(DatabricksUCClient)

    def _raise_404(path, params=None):
        raise httpx.HTTPStatusError(
            "not found",
            request=httpx.Request("GET", path),
            response=httpx.Response(404, request=httpx.Request("GET", path)),
        )

    monkeypatch.setattr(client, "_get", _raise_404)
    assert client.list_policies("catalog", "main") == []


def test_collect_dbx_rows_materializes_abac_with_warehouse(monkeypatch) -> None:
    """With a warehouse configured, ABAC policies resolve to concrete DAS rows."""
    from app.services import abac_resolver

    class FakeClient:
        def __init__(self, _url):
            pass

        def get_grants(self, securable_type, full_name):
            return []

        def list_schemas(self, catalog):
            return [{"name": "gold"}]

        def list_tables(self, catalog, schema):
            return [{"name": "orders"}]

        def get_table(self, full_name):
            return {
                "columns": [
                    {"name": "id"},
                    {"name": "ssn"},
                ]
            }

        def list_policies(self, securable_type, full_name, include_inherited=False):
            if securable_type == "catalog":
                return [
                    {
                        "id": "p1",
                        "name": "mask_ssn",
                        "policy_type": "POLICY_TYPE_COLUMN_MASK",
                        "on_securable_type": "CATALOG",
                        "on_securable_fullname": "main",
                        "column_mask": {"function_name": "main.gov.mask", "on_column": "c"},
                        "match_columns": [
                            {"alias": "c", "condition": "has_tag_value('pii','ssn')"}
                        ],
                        "to_principals": ["analysts"],
                    }
                ]
            return []

        def get_function(self, full_name):
            return {}

    monkeypatch.setattr(pair_diff, "DatabricksUCClient", FakeClient)
    monkeypatch.setattr(pair_diff.settings, "DBX_WAREHOUSE_ID", "wh-123")
    monkeypatch.setattr(
        pair_diff.abac_resolver,
        "read_governed_tags",
        lambda _client, _wh, _cat: abac_resolver.GovernedTags(
            column={("gold", "orders", "ssn"): {"pii": "ssn"}}
        ),
    )

    rows = collect_dbx_rows("https://adb-123.4.azuredatabricks.net", "main")
    resolved = [
        r for r in rows
        if r.constraint_details.get("source") == "databricks_abac_resolved"
    ]
    assert len(resolved) == 1
    row = resolved[0]
    assert row.constraint_kind == "column_mask"
    assert row.securable_scope == "table:main.gold.orders"
    assert row.constraint_details["column"] == "ssn"
    assert row.constraint_details["materialized"] is True
    # Resolved rows must NOT be tagged as abstract (so apply does not skip them).
    assert row.constraint_details["source"] != "databricks_abac"


def test_resolved_abac_row_applies_via_normal_path(monkeypatch) -> None:
    """A resolved ABAC row flows through the Fabric DAS apply path (not skipped)."""
    captured = {}

    monkeypatch.setattr(pair_apply, "_find_mirrored_catalog_item", lambda *_a, **_k: "item-1")

    def _fake_grant(*_a, **_k):
        captured["called"] = True
        return True, False, "granted (Fabric DAS column_mask)"

    monkeypatch.setattr(pair_apply, "_grant_fabric_constraint", _fake_grant)

    report = apply_rows(
        {
            "dbx_workspace_url": "https://adb.example.azuredatabricks.net",
            "uc_catalog": "main",
            "fabric_workspace_id": "43fa6fa9-d863-e811-a838-000d3a309c3d",
        },
        [
            DiffRow(
                principal_key=TABLE_POLICY_PRINCIPAL,
                principal_display="Table policy",
                principal_type="Policy",
                securable_scope="table:main.gold.orders",
                access_class="DATA_READ",
                on_dbx=True,
                constraint_kind="column_mask",
                constraint_key="abac-resolved-1",
                constraint_details={
                    "source": "databricks_abac_resolved",
                    "policy_name": "mask_ssn",
                    "function": "main.gov.mask",
                    "column": "ssn",
                    "table": "main.gold.orders",
                    "materialized": True,
                },
            )
        ],
        "dbx_to_fabric",
        dry_run=False,
    )

    action = report.actions[0]
    assert captured.get("called") is True
    assert action.ok is True
    assert action.skipped is False


def test_abac_resolved_dedupes_into_effective_mask_with_provenance(monkeypatch) -> None:
    """When an active ABAC policy also surfaces via the table's effective masks,
    the resolved target dedupes into that row but annotates ABAC provenance."""
    from app.services import abac_resolver

    class FakeClient:
        def __init__(self, _url):
            pass

        def get_grants(self, securable_type, full_name):
            return []

        def list_schemas(self, catalog):
            return [{"name": "gold"}]

        def list_tables(self, catalog, schema):
            return [{"name": "orders"}]

        def get_table(self, full_name):
            # Active ABAC policy shows up as an effective mask on the column.
            return {
                "columns": [
                    {"name": "id"},
                    {
                        "name": "ssn",
                        "effective_masks": [{"function_name": "main.gov.mask"}],
                    },
                ]
            }

        def get_function(self, full_name):
            return {}

        def list_policies(self, securable_type, full_name, include_inherited=False):
            if securable_type == "catalog":
                return [
                    {
                        "id": "p1",
                        "name": "mask_ssn",
                        "policy_type": "POLICY_TYPE_COLUMN_MASK",
                        "on_securable_type": "CATALOG",
                        "on_securable_fullname": "main",
                        "column_mask": {"function_name": "main.gov.mask", "on_column": "c"},
                        "match_columns": [
                            {"alias": "c", "condition": "has_tag_value('pii','ssn')"}
                        ],
                        "to_principals": ["analysts"],
                    }
                ]
            return []

    monkeypatch.setattr(pair_diff, "DatabricksUCClient", FakeClient)
    monkeypatch.setattr(pair_diff.settings, "DBX_WAREHOUSE_ID", "wh-123")
    monkeypatch.setattr(
        pair_diff.abac_resolver,
        "read_governed_tags",
        lambda _client, _wh, _cat: abac_resolver.GovernedTags(
            column={("gold", "orders", "ssn"): {"pii": "ssn"}}
        ),
    )

    rows = collect_dbx_rows("https://adb-123.4.azuredatabricks.net", "main")
    masks = [
        r for r in rows
        if r.constraint_kind == "column_mask"
        and r.constraint_details.get("column") == "ssn"
    ]
    # Single deduped row carrying ABAC provenance (identical Fabric constraint).
    assert len(masks) == 1
    assert masks[0].constraint_details["abac_driven"] is True
    assert masks[0].constraint_details["abac_policy"] == "mask_ssn"


def test_constraint_selection_key_keeps_rows_distinct() -> None:
    base = DiffRow(
        principal_key="00000000-0000-0000-0000-000000000001",
        principal_display="user",
        principal_type="User",
        securable_scope="table:main.gold.orders",
        access_class="DATA_READ",
    )
    constrained = DiffRow(
        principal_key=base.principal_key,
        principal_display=base.principal_display,
        principal_type=base.principal_type,
        securable_scope=base.securable_scope,
        access_class=base.access_class,
        constraint_kind="row_filter",
        constraint_key="abc",
    )

    assert base.selection_key != constrained.selection_key


def test_collect_fabric_rows_requires_exact_mirrored_catalog(monkeypatch) -> None:
    monkeypatch.setattr(pair_diff.fabric_rest, "list_role_assignments", lambda _workspace_id: [])
    monkeypatch.setattr(
        pair_diff.fabric_rest,
        "list_items",
        lambda _workspace_id: [
            {
                "id": "22222222-2222-2222-2222-222222222222",
                "type": "MirroredAzureDatabricksCatalog",
                "displayName": "other_catalog",
            }
        ],
    )

    def _raise_if_called(*_args, **_kwargs):
        raise AssertionError("should not read DARs from an unrelated mirrored catalog")

    monkeypatch.setattr(pair_diff.fabric_rest, "list_data_access_policies", _raise_if_called)

    assert collect_fabric_rows(
        "43fa6fa9-d863-e811-a838-000d3a309c3d",
        "main",
    ) == []


def test_collect_fabric_rows_ignores_same_name_lakehouse(monkeypatch) -> None:
    monkeypatch.setattr(pair_diff.fabric_rest, "list_role_assignments", lambda _workspace_id: [])
    monkeypatch.setattr(
        pair_diff.fabric_rest,
        "list_items",
        lambda _workspace_id: [
            {
                "id": "33333333-3333-3333-3333-333333333333",
                "type": "Lakehouse",
                "displayName": "main",
            }
        ],
    )

    def _raise_if_called(*_args, **_kwargs):
        raise AssertionError("should not read DARs from a same-name Lakehouse")

    monkeypatch.setattr(pair_diff.fabric_rest, "list_data_access_policies", _raise_if_called)

    assert collect_fabric_rows(
        "43fa6fa9-d863-e811-a838-000d3a309c3d",
        "main",
    ) == []


def test_collect_fabric_rows_raises_on_dar_read_failure(monkeypatch) -> None:
    _patch_fabric_dar(monkeypatch, [])

    def _raise_dar_failure(*_args, **_kwargs):
        request = httpx.Request("GET", "https://api.fabric.microsoft.com/v1/test")
        response = httpx.Response(403, request=request)
        raise httpx.HTTPStatusError("forbidden", request=request, response=response)

    monkeypatch.setattr(pair_diff.fabric_rest, "list_data_access_policies", _raise_dar_failure)

    with pytest.raises(httpx.HTTPStatusError):
        collect_fabric_rows(
            "43fa6fa9-d863-e811-a838-000d3a309c3d",
            "main",
        )


def test_collect_fabric_rows_does_not_leak_paths_across_rules(monkeypatch) -> None:
    principal_id = "00000000-0000-0000-0000-000000000001"
    _patch_fabric_dar(
        monkeypatch,
        [
            {
                "members": {
                    "microsoftEntraMembers": [
                        {"objectId": principal_id, "objectType": "User"}
                    ]
                },
                "decisionRules": [
                    {
                        "permission": [
                            {
                                "attributeName": "Path",
                                "attributeValueIncludedIn": ["/Tables/gold/orders"],
                            },
                            {
                                "attributeName": "Action",
                                "attributeValueIncludedIn": ["Read"],
                            },
                        ]
                    },
                    {
                        "permission": [
                            {
                                "attributeName": "Path",
                                "attributeValueIncludedIn": ["/Tables/gold/payroll"],
                            },
                            {
                                "attributeName": "Action",
                                "attributeValueIncludedIn": ["Write"],
                            },
                        ]
                    },
                ],
            }
        ],
    )

    rows = collect_fabric_rows(
        "43fa6fa9-d863-e811-a838-000d3a309c3d",
        "main",
    )
    scopes = {(row.securable_scope, row.access_class) for row in rows}

    assert ("table:main.gold.orders", "DATA_READ") in scopes
    assert ("table:main.gold.payroll", "DATA_WRITE") in scopes
    assert ("table:main.gold.orders", "DATA_WRITE") not in scopes


def test_databricks_get_grants_raises_on_auth_failure() -> None:
    client = object.__new__(DatabricksUCClient)

    def _raise_status(_path):
        request = httpx.Request("GET", "https://adb-123.4.azuredatabricks.net/test")
        response = httpx.Response(403, request=request)
        raise httpx.HTTPStatusError("forbidden", request=request, response=response)

    client._get = _raise_status

    with pytest.raises(httpx.HTTPStatusError):
        client.get_grants("catalog", "main")


def test_databricks_get_grants_returns_empty_on_not_found() -> None:
    client = object.__new__(DatabricksUCClient)

    def _raise_status(_path):
        request = httpx.Request("GET", "https://adb-123.4.azuredatabricks.net/test")
        response = httpx.Response(404, request=request)
        raise httpx.HTTPStatusError("not found", request=request, response=response)

    client._get = _raise_status

    assert client.get_grants("catalog", "main") == []
