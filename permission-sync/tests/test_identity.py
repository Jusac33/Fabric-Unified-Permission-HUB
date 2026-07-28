"""Verify that IdentityProvider never receives raw secrets as constructor input
and that credential selection is driven purely by environment variables."""
import os
from unittest.mock import patch, MagicMock

from src.auth.identity import IdentityProvider


def test_credential_uses_default_locally(monkeypatch):
    monkeypatch.delenv("RUNNING_IN_AZURE", raising=False)
    monkeypatch.setenv("KEY_VAULT_URI", "https://kv.example/")
    ip = IdentityProvider()
    with patch("src.auth.identity.DefaultAzureCredential") as dac, \
         patch("src.auth.identity.ManagedIdentityCredential") as mic:
        dac.return_value = MagicMock(name="DAC")
        ip.get_credential()
        assert dac.called
        assert not mic.called


def test_credential_uses_mi_in_azure(monkeypatch):
    monkeypatch.setenv("RUNNING_IN_AZURE", "true")
    monkeypatch.setenv("AZURE_CLIENT_ID", "00000000-0000-0000-0000-000000000000")
    monkeypatch.setenv("KEY_VAULT_URI", "https://kv.example/")
    ip = IdentityProvider()
    with patch("src.auth.identity.ManagedIdentityCredential") as mic, \
         patch("src.auth.identity.DefaultAzureCredential") as dac:
        mic.return_value = MagicMock(name="MIC")
        ip.get_credential()
        mic.assert_called_once()
        assert not dac.called


def test_no_secrets_in_constructor(monkeypatch):
    """No constructor argument accepts a secret value."""
    import inspect
    sig = inspect.signature(IdentityProvider.__init__)
    for name in sig.parameters:
        assert "secret" not in name.lower()
        assert "password" not in name.lower()
        assert "token" not in name.lower()
