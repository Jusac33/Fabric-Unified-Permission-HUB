from __future__ import annotations

import pytest

from app.config import Settings


def test_development_allows_default_secret() -> None:
    settings = Settings(APP_ENV="development", APP_DEBUG=False, SECRET_KEY="change-me")

    settings.validate_runtime_security()


def test_production_rejects_debug() -> None:
    settings = Settings(APP_ENV="production", APP_DEBUG=True, SECRET_KEY="not-default")

    with pytest.raises(RuntimeError, match="APP_DEBUG"):
        settings.validate_runtime_security()


def test_production_rejects_default_secret() -> None:
    settings = Settings(APP_ENV="production", APP_DEBUG=False, SECRET_KEY="change-me")

    with pytest.raises(RuntimeError, match="SECRET_KEY"):
        settings.validate_runtime_security()
