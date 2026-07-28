from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.validation import (
    is_databricks_workspace_url,
    is_https_url,
    normalize_databricks_workspace_url,
    require_pairing_id,
    require_safe_path_segment,
    require_uuid,
)


def test_require_uuid_accepts_valid_uuid() -> None:
    value = "43fa6fa9-d863-e811-a838-000d3a309c3d"

    assert require_uuid(value, "workspace ID") == value


def test_require_uuid_rejects_path_injection() -> None:
    with pytest.raises(HTTPException):
        require_uuid("../not-a-guid", "workspace ID")


def test_require_pairing_id_accepts_ten_hex_chars() -> None:
    assert require_pairing_id("a1b2c3d4e5") == "a1b2c3d4e5"


def test_require_safe_path_segment_rejects_slash() -> None:
    with pytest.raises(HTTPException):
        require_safe_path_segment("catalog/other", "catalog")


def test_is_https_url_requires_https() -> None:
    assert is_https_url("https://adb-123.azuredatabricks.net")
    assert not is_https_url("http://adb-123.azuredatabricks.net")


def test_is_databricks_workspace_url_requires_azure_databricks_host() -> None:
    assert is_databricks_workspace_url("https://adb-123.4.azuredatabricks.net")
    assert not is_databricks_workspace_url("https://attacker.example")
    assert not is_databricks_workspace_url("http://adb-123.4.azuredatabricks.net")


def test_normalize_databricks_workspace_url_strips_browser_query() -> None:
    assert normalize_databricks_workspace_url(
        "https://adb-123.4.azuredatabricks.net/?o=123#workspace"
    ) == "https://adb-123.4.azuredatabricks.net"


def test_normalize_databricks_workspace_url_rejects_invalid_port() -> None:
    assert normalize_databricks_workspace_url(
        "https://adb-123.4.azuredatabricks.net:bad"
    ) is None
