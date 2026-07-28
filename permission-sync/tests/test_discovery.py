"""Discovery tests — all HTTP calls mocked."""
from unittest.mock import MagicMock, patch

from src.discovery.fabric_discovery import FabricDiscovery


def _resp(payload, status=200):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = payload
    r.raise_for_status = lambda: None
    return r


def _cfg():
    return {"discovery": {"fabric": {
        "api_base": "https://api.fabric.microsoft.com/v1",
        "workspace_filter_tag_key": "",
        "workspace_filter_tag_value": "",
        "onelake_data_access_roles_enabled": False,
    }}}


def test_fabric_discover_returns_canonical_perms():
    idp = MagicMock(); idp.get_fabric_token.return_value = "t"
    normalizer = MagicMock(); normalizer.normalize.return_value = "DATA_READ"
    fd = FabricDiscovery(_cfg(), idp, normalizer, run_id="run-1")

    workspaces = _resp({"value": [{"id": "ws-1", "displayName": "WS1"}]})
    roles = _resp({"value": [{
        "principal": {"id": "oid-1", "type": "User", "displayName": "Alice"},
        "role": "Viewer",
    }]})
    empty = _resp({"value": []})

    with patch("httpx.get", side_effect=[workspaces, roles, empty, empty]):
        perms = fd.discover_all()

    assert len(perms) == 1
    p = perms[0]
    assert p.principal_entra_id == "oid-1"
    assert p.access_class == "DATA_READ"
    assert p.securable_type == "workspace"
    assert p.source_platform == "fabric"
    assert p.sync_run_id == "run-1"


def test_fabric_discover_skips_unmapped_privileges():
    idp = MagicMock(); idp.get_fabric_token.return_value = "t"
    normalizer = MagicMock(); normalizer.normalize.return_value = None  # unmapped
    fd = FabricDiscovery(_cfg(), idp, normalizer, run_id="r")

    workspaces = _resp({"value": [{"id": "ws-1", "displayName": "WS1"}]})
    roles = _resp({"value": [{"principal": {"id": "oid", "type": "User"}, "role": "Mystery"}]})
    with patch("httpx.get", side_effect=[workspaces, roles]):
        assert fd.discover_all() == []
