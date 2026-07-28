"""Identity resolution is fully mocked — no real Graph calls."""
from unittest.mock import MagicMock, patch

from src.translation.identity_resolver import IdentityResolver, EntraIdentity


def _make_resolver():
    idp = MagicMock()
    idp.get_graph_token.return_value = "fake-token"
    return IdentityResolver(idp)


def _graph_response(rows):
    r = MagicMock()
    r.status_code = 200
    r.json.return_value = {"value": rows}
    r.raise_for_status = lambda: None
    return r


def test_resolve_uc_upn_to_user():
    r = _make_resolver()
    user = {"id": "uid-1", "userPrincipalName": "alice@contoso.com", "displayName": "Alice"}
    with patch("httpx.get", return_value=_graph_response([user])):
        ent = r.resolve_to_entra_id("alice@contoso.com", "unity_catalog")
    assert isinstance(ent, EntraIdentity)
    assert ent.object_id == "uid-1"
    assert ent.principal_type == "User"


def test_resolve_unknown_returns_none():
    r = _make_resolver()
    with patch("httpx.get", return_value=_graph_response([])):
        assert r.resolve_to_entra_id("ghost@contoso.com", "unity_catalog") is None


def test_fabric_guid_shortcircuits_graph_call():
    r = _make_resolver()
    guid = "11111111-2222-3333-4444-555555555555"
    with patch("httpx.get") as g:
        ent = r.resolve_to_entra_id(guid, "fabric")
    assert ent is not None and ent.object_id == guid
    # No Graph call was needed for a pre-resolved Entra GUID
    assert g.call_count == 0


def test_cache_hit_avoids_duplicate_calls():
    r = _make_resolver()
    user = {"id": "uid-1", "userPrincipalName": "x@y", "displayName": "X"}
    with patch("httpx.get", return_value=_graph_response([user])) as g:
        r.resolve_to_entra_id("x@y", "unity_catalog")
        r.resolve_to_entra_id("x@y", "unity_catalog")
    assert g.call_count == 1
