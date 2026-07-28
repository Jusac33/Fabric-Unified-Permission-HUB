"""Round-trip safety for the Fabric <-> Unity Catalog access-class mapping.

A sync that changes effective access while claiming to mirror it is worse than
no sync at all, so the mapping between access classes and Fabric workspace roles
must not widen privileges when a permission travels in both directions.
"""
from __future__ import annotations

from app.services.pair_apply import ACCESS_TO_FABRIC_ROLE
from app.services.pair_diff import FABRIC_ROLE_TO_ACCESS


def test_workspace_role_mapping_round_trips_without_widening() -> None:
    """access -> Fabric role -> access must return the same class.

    Regression: OBJECT_USE and DATA_READ both mapped to "Viewer" while Viewer
    mapped back to DATA_READ, so a Unity Catalog USE_CATALOG grant re-entered UC
    as USE_CATALOG + SELECT after a full round trip.
    """
    for access_class, role in ACCESS_TO_FABRIC_ROLE.items():
        assert FABRIC_ROLE_TO_ACCESS.get(role) == access_class, (
            f"{access_class} -> {role} -> {FABRIC_ROLE_TO_ACCESS.get(role)} widens access"
        )


def test_forward_mapping_is_injective() -> None:
    """Two access classes sharing one Fabric role make the reverse ambiguous."""
    roles = list(ACCESS_TO_FABRIC_ROLE.values())
    assert len(roles) == len(set(roles)), f"ambiguous reverse mapping: {roles}"


def test_traverse_privilege_has_no_workspace_role() -> None:
    """USE_CATALOG / USE_SCHEMA grant no data access and must not become a role."""
    assert "OBJECT_USE" not in ACCESS_TO_FABRIC_ROLE
