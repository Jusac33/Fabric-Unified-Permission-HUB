from src.model.canonical_permission import CanonicalPermission, DATA_READ, DATA_WRITE
from src.reconciler.diff_engine import compute_diff


def _p(entra_id, access, fqn="db.schema.t", platform="unity_catalog"):
    return CanonicalPermission(
        principal_entra_id=entra_id,
        principal_type="User",
        access_class=access,
        securable_type="table",
        securable_fqn=fqn,
        source_platform=platform,
    )


def test_to_grant_when_present_in_desired_only():
    desired = [_p("u1", DATA_READ)]
    current = []
    d = compute_diff(current_state=current, desired_state=desired)
    assert len(d.to_grant) == 1
    assert d.to_revoke == []
    assert d.conflicts == []


def test_to_revoke_when_present_in_current_only():
    desired = []
    current = [_p("u1", DATA_READ, platform="fabric")]
    d = compute_diff(current_state=current, desired_state=desired)
    assert len(d.to_revoke) == 1
    assert d.to_grant == []


def test_unchanged_when_same_key():
    p = _p("u1", DATA_READ)
    d = compute_diff(current_state=[p], desired_state=[p])
    assert d.to_grant == [] and d.to_revoke == []
    assert len(d.unchanged) == 1


def test_conflict_detected_when_different_access_class():
    src = _p("u1", DATA_WRITE)
    tgt = _p("u1", DATA_READ, platform="fabric")
    d = compute_diff(current_state=[tgt], desired_state=[src])
    # Two different keys (different access_class) → grant + revoke
    assert len(d.to_grant) == 1
    assert len(d.to_revoke) == 1
    # And a conflict is recorded
    assert len(d.conflicts) == 1
    c = d.conflicts[0]
    assert c.principal_entra_id == "u1"
    assert c.source_access_class == DATA_WRITE
    assert c.target_access_class == DATA_READ
