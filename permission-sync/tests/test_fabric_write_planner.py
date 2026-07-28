"""FabricWritePlanner is the ONLY place that chooses a Fabric security layer."""
from unittest.mock import MagicMock

from src.model.canonical_permission import (
    CanonicalPermission, DATA_READ, WORKSPACE_ADMIN,
)
from src.writers.fabric_write_planner import FabricWritePlanner


def _normalizer(mapping):
    m = MagicMock()
    m.denormalize.side_effect = lambda ac, target: mapping.get((ac, target))
    return m


def _perm(access, securable_type="workspace"):
    return CanonicalPermission(
        principal_entra_id="oid-1", principal_type="User",
        access_class=access, securable_type=securable_type,
        securable_fqn="ws1", workspace_id="ws-1", source_platform="fabric",
    )


def test_workspace_primary_used_by_default():
    planner_cfg = {
        DATA_READ: {"primary_layer": "workspace_role", "fallback_layer": "workspace_role"},
    }
    n = _normalizer({(DATA_READ, "fabric_workspace_role"): "Viewer"})
    p = FabricWritePlanner(planner_cfg, {"onelake_data_access_roles_enabled": False}, n)
    plan = p.plan(_perm(DATA_READ))
    assert plan.layer == "workspace_role"
    assert plan.role_or_action == "Viewer"


def test_onelake_primary_falls_back_when_disabled():
    planner_cfg = {
        DATA_READ: {"primary_layer": "onelake_data_access_role",
                    "fallback_layer": "workspace_role"},
    }
    n = _normalizer({(DATA_READ, "fabric_workspace_role"): "Viewer"})
    p = FabricWritePlanner(planner_cfg, {"onelake_data_access_roles_enabled": False}, n)
    plan = p.plan(_perm(DATA_READ, "lakehouse"))
    assert plan.layer == "workspace_role"


def test_onelake_primary_used_when_enabled():
    planner_cfg = {
        DATA_READ: {"primary_layer": "onelake_data_access_role",
                    "fallback_layer": "workspace_role"},
    }
    n = _normalizer({(DATA_READ, "fabric_onelake_role"): "Reader"})
    p = FabricWritePlanner(planner_cfg, {"onelake_data_access_roles_enabled": True}, n)
    plan = p.plan(_perm(DATA_READ, "lakehouse"))
    assert plan.layer == "onelake_data_access_role"
    assert plan.role_or_action == "Reader"


def test_none_layer_produces_skip():
    planner_cfg = {
        WORKSPACE_ADMIN: {"primary_layer": "none", "fallback_layer": "none"},
    }
    n = _normalizer({})
    p = FabricWritePlanner(planner_cfg, {"onelake_data_access_roles_enabled": False}, n)
    plan = p.plan(_perm(WORKSPACE_ADMIN))
    assert plan.layer == "skip"
