"""End-to-end sync engine test with all I/O mocked."""
import asyncio
from unittest.mock import MagicMock, patch

from src.model.canonical_permission import CanonicalPermission, DATA_READ
from src.orchestrator.sync_engine import SyncEngine


CONFIG = {
    "sync": {"dry_run": True, "direction": "source_to_fabric",
             "source_platform": "unity_catalog",
             "fabric_write_planner": {
                 DATA_READ: {"primary_layer": "workspace_role",
                             "fallback_layer": "workspace_role"},
             }},
    "privilege_matrix_path": "config/privilege_matrix.yaml",
    "conflict_resolution": {"strategy": "source_wins"},
    "discovery": {"fabric": {"api_base": "", "onelake_data_access_roles_enabled": False}},
    "onesync": {"enabled": False},
    "event_accelerators": {"enabled": False},
    "observability": {"audit_dir": "logs_test"},
}


def _perm(access=DATA_READ, source_platform="unity_catalog"):
    return CanonicalPermission(
        principal_entra_id="oid-1", principal_type="User",
        access_class=access, securable_type="table",
        securable_fqn="db.schema.t", source_platform=source_platform,
    )


def test_full_reconcile_dispatches_grant(tmp_path):
    with patch("src.orchestrator.sync_engine.load_config", return_value=CONFIG), \
         patch("src.orchestrator.sync_engine.IdentityProvider") as IP, \
         patch("src.orchestrator.sync_engine.PrivilegeNormalizer") as PN, \
         patch("src.orchestrator.sync_engine.IdentityResolver"), \
         patch("src.orchestrator.sync_engine.AuditLogger") as AL, \
         patch("src.orchestrator.sync_engine.ConflictResolver") as CR, \
         patch("src.orchestrator.sync_engine.PolicyWeaverClient") as PW, \
         patch("src.orchestrator.sync_engine.OneSyncClient"), \
         patch("src.orchestrator.sync_engine.OneSyncOrchestrator"):
        IP.return_value = MagicMock()
        PN.return_value = MagicMock()
        audit = MagicMock(); audit.run_id = "r"
        AL.return_value = audit
        CR.return_value.resolve_all.return_value = []
        apply_result = MagicMock(granted=[{"x": 1}], revoked=[], skipped=[], errors=[])
        PW.return_value.apply_diff.return_value = apply_result

        engine = SyncEngine("fake.yaml")

        # Replace discovery with canned lists
        engine._discover_source_sync = lambda: [_perm()]  # type: ignore
        engine._discover_target_sync = lambda: []         # type: ignore

        result = asyncio.run(engine.run())

    assert result["source_count"] == 1
    assert result["target_count"] == 0
    assert result["to_grant"] == 1
    assert result["granted"] == 1
    assert result["dry_run"] is True


def test_event_mode_falls_back_when_disabled():
    with patch("src.orchestrator.sync_engine.load_config", return_value=CONFIG), \
         patch("src.orchestrator.sync_engine.IdentityProvider"), \
         patch("src.orchestrator.sync_engine.PrivilegeNormalizer"), \
         patch("src.orchestrator.sync_engine.IdentityResolver"), \
         patch("src.orchestrator.sync_engine.AuditLogger") as AL, \
         patch("src.orchestrator.sync_engine.ConflictResolver") as CR, \
         patch("src.orchestrator.sync_engine.PolicyWeaverClient") as PW, \
         patch("src.orchestrator.sync_engine.OneSyncClient"), \
         patch("src.orchestrator.sync_engine.OneSyncOrchestrator"):
        AL.return_value = MagicMock(run_id="r")
        CR.return_value.resolve_all.return_value = []
        PW.return_value.apply_diff.return_value = MagicMock(
            granted=[], revoked=[], skipped=[], errors=[])
        engine = SyncEngine("fake.yaml", mode="event_accelerator")
        engine._discover_source_sync = lambda: []  # type: ignore
        engine._discover_target_sync = lambda: []  # type: ignore
        result = asyncio.run(engine.run())
    # Still returns a summary — event mode fell back to full_reconcile
    assert result["mode"] == "event_accelerator"
    assert result["source_count"] == 0
