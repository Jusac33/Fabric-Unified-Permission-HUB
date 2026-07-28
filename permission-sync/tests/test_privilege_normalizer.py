"""Privileges come from YAML; changing the YAML changes behavior with no code change."""
from pathlib import Path

from src.translation.privilege_normalizer import PrivilegeNormalizer


def _write_matrix(tmp_path: Path, extra_uc: dict = None) -> Path:
    import yaml
    extra_uc = extra_uc or {}
    data = {
        "normalize": {
            "unity_catalog": {"SELECT": "DATA_READ", "MODIFY": "DATA_WRITE", **extra_uc},
            "snowflake": {"SELECT": "DATA_READ"},
        },
        "denormalize": {
            "fabric_workspace_role": {"DATA_READ": "Viewer", "DATA_WRITE": "Contributor"},
            "fabric_onelake_role": {"DATA_READ": "Reader"},
        },
    }
    p = tmp_path / "matrix.yaml"
    p.write_text(yaml.safe_dump(data))
    return p


def test_normalize_known(tmp_path):
    n = PrivilegeNormalizer(_write_matrix(tmp_path))
    assert n.normalize("SELECT", "unity_catalog") == "DATA_READ"
    assert n.normalize("select", "unity_catalog") == "DATA_READ"


def test_normalize_unknown_returns_none(tmp_path, caplog):
    n = PrivilegeNormalizer(_write_matrix(tmp_path))
    assert n.normalize("EXECUTE_UDF", "unity_catalog") is None


def test_denormalize_routes_per_layer(tmp_path):
    n = PrivilegeNormalizer(_write_matrix(tmp_path))
    assert n.denormalize("DATA_READ", "fabric_workspace_role") == "Viewer"
    assert n.denormalize("DATA_READ", "fabric_onelake_role") == "Reader"


def test_yaml_changes_picked_up_without_code_change(tmp_path):
    """Change the YAML, reload: behavior changes — proves zero-hardcoded-privileges."""
    p = _write_matrix(tmp_path)
    n1 = PrivilegeNormalizer(p)
    assert n1.normalize("USAGE", "unity_catalog") is None

    # Add a new mapping by editing YAML — no Python code is touched
    import yaml
    data = yaml.safe_load(p.read_text())
    data["normalize"]["unity_catalog"]["USAGE"] = "OBJECT_USE"
    p.write_text(yaml.safe_dump(data))

    n2 = PrivilegeNormalizer(p)
    assert n2.normalize("USAGE", "unity_catalog") == "OBJECT_USE"
