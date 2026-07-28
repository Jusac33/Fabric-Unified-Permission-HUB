from __future__ import annotations

import httpx
import pytest

from app.services import fabric_rest


def test_list_role_assignments_raises_on_auth_failure(monkeypatch) -> None:
    class FakeClient:
        def get(self, *_args, **_kwargs):
            return httpx.Response(403, request=httpx.Request("GET", "https://example.test"))

    monkeypatch.setattr(fabric_rest, "_client", FakeClient())
    monkeypatch.setattr(fabric_rest, "get_fabric_token", lambda: "token")

    with pytest.raises(httpx.HTTPStatusError):
        fabric_rest.list_role_assignments("43fa6fa9-d863-e811-a838-000d3a309c3d")


def test_list_data_access_policies_raises_on_not_found(monkeypatch) -> None:
    class FakeClient:
        def get(self, *_args, **_kwargs):
            return httpx.Response(404, request=httpx.Request("GET", "https://example.test"))

    monkeypatch.setattr(fabric_rest, "_client", FakeClient())
    monkeypatch.setattr(fabric_rest, "get_fabric_token", lambda: "token")

    with pytest.raises(httpx.HTTPStatusError):
        fabric_rest.list_data_access_policies(
            "43fa6fa9-d863-e811-a838-000d3a309c3d",
            "11111111-1111-1111-1111-111111111111",
        )


def test_list_shortcuts_calls_item_shortcuts_endpoint(monkeypatch) -> None:
    calls = []

    class FakeClient:
        def get(self, *args, **kwargs):
            calls.append((args, kwargs))
            return httpx.Response(
                200,
                request=httpx.Request("GET", "https://example.test"),
                json={"value": [{"name": "s3", "target": {"amazonS3": {"bucketName": "b"}}}]},
            )

    monkeypatch.setattr(fabric_rest, "_client", FakeClient())
    monkeypatch.setattr(fabric_rest, "get_fabric_token", lambda: "token")

    rows = fabric_rest.list_shortcuts(
        "43fa6fa9-d863-e811-a838-000d3a309c3d",
        "11111111-1111-1111-1111-111111111111",
        path="Tables",
    )

    assert rows == [{"name": "s3", "target": {"amazonS3": {"bucketName": "b"}}}]
    assert calls[0][0] == (
        "/workspaces/43fa6fa9-d863-e811-a838-000d3a309c3d/items/11111111-1111-1111-1111-111111111111/shortcuts",
    )
    assert calls[0][1]["params"] == {"path": "Tables"}


def test_list_workspace_shortcuts_only_reads_lakehouse_items(monkeypatch) -> None:
    monkeypatch.setattr(
        fabric_rest,
        "list_items",
        lambda _workspace_id: [
            {"id": "lakehouse-id", "type": "Lakehouse", "displayName": "LH"},
            {"id": "report-id", "type": "Report", "displayName": "R"},
        ],
    )
    monkeypatch.setattr(
        fabric_rest,
        "list_shortcuts",
        lambda _workspace_id, item_id: [{"name": item_id, "target": {}}],
    )

    rows = fabric_rest.list_workspace_shortcuts("43fa6fa9-d863-e811-a838-000d3a309c3d")

    assert rows == [
        {
            "item": {"id": "lakehouse-id", "type": "Lakehouse", "displayName": "LH"},
            "shortcut": {"name": "lakehouse-id", "target": {}},
        }
    ]
