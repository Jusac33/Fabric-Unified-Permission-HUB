"""Identity provider — single source of truth for auth across the system.

Auth tier: Tier 1 (secretless via Managed Identity / DefaultAzureCredential)
           Tier 2 (Key Vault fallback) for platforms that require secrets.
Tier 3 is forbidden; no secrets in code or env.
"""
from __future__ import annotations
import logging
import os
from threading import Lock
from typing import Dict, Optional

from azure.identity import DefaultAzureCredential, ManagedIdentityCredential
from azure.core.credentials import TokenCredential
from azure.keyvault.secrets import SecretClient

log = logging.getLogger(__name__)


class IdentityProvider:
    """
    Auth tier: Tier 1 for Azure resources (MI / DefaultAzureCredential);
    Tier 2 for Snowflake credentials which are loaded via Key Vault at runtime.
    """

    _SCOPE_FABRIC = "https://analysis.windows.net/powerbi/api/.default"
    _SCOPE_DATABRICKS = "2ff814a6-3304-4ab8-85cb-cd0e6f879c1d/.default"
    _SCOPE_GRAPH = "https://graph.microsoft.com/.default"
    _SCOPE_ARM = "https://management.azure.com/.default"
    _SCOPE_STORAGE = "https://storage.azure.com/.default"

    def __init__(self) -> None:
        self._running_in_azure = os.getenv("RUNNING_IN_AZURE", "false").lower() == "true"
        self._azure_client_id = os.getenv("AZURE_CLIENT_ID")
        self._key_vault_uri = os.getenv("KEY_VAULT_URI")
        if not self._key_vault_uri:
            log.warning("KEY_VAULT_URI not set; get_secret() will fail until it is.")
        self._credential: Optional[TokenCredential] = None
        self._secret_client: Optional[SecretClient] = None
        self._secret_cache: Dict[str, str] = {}
        self._lock = Lock()

    # --- credential ---
    def get_credential(self) -> TokenCredential:
        if self._credential is not None:
            return self._credential
        with self._lock:
            if self._credential is not None:
                return self._credential
            if self._running_in_azure:
                if self._azure_client_id:
                    log.info("Using user-assigned Managed Identity")
                    self._credential = ManagedIdentityCredential(client_id=self._azure_client_id)
                else:
                    log.info("Using system-assigned Managed Identity")
                    self._credential = ManagedIdentityCredential()
            else:
                log.info("Using DefaultAzureCredential (local dev, az login)")
                self._credential = DefaultAzureCredential(
                    exclude_interactive_browser_credential=True,
                )
        return self._credential

    # --- tokens ---
    def get_token(self, scope: str) -> str:
        return self.get_credential().get_token(scope).token

    def get_fabric_token(self) -> str:
        return self.get_token(self._SCOPE_FABRIC)

    def get_databricks_token(self) -> str:
        return self.get_token(self._SCOPE_DATABRICKS)

    def get_graph_token(self) -> str:
        return self.get_token(self._SCOPE_GRAPH)

    def get_arm_token(self) -> str:
        return self.get_token(self._SCOPE_ARM)

    def get_storage_token(self) -> str:
        return self.get_token(self._SCOPE_STORAGE)

    # --- Key Vault ---
    def _kv(self) -> SecretClient:
        if self._secret_client is None:
            if not self._key_vault_uri:
                raise RuntimeError("KEY_VAULT_URI env var is required to read secrets")
            self._secret_client = SecretClient(
                vault_url=self._key_vault_uri, credential=self.get_credential()
            )
        return self._secret_client

    def get_secret(self, secret_name: str) -> str:
        if secret_name in self._secret_cache:
            return self._secret_cache[secret_name]
        try:
            value = self._kv().get_secret(secret_name).value or ""
        except Exception as e:
            raise RuntimeError(f"Secret '{secret_name}' not found in Key Vault: {e}") from e
        self._secret_cache[secret_name] = value
        return value

    def list_secret_names(self, prefix: str) -> list[str]:
        return [
            p.name for p in self._kv().list_properties_of_secrets()
            if p.name and p.name.startswith(prefix)
        ]

    # --- Snowflake bundle (Tier 2) ---
    def get_snowflake_auth(self, account_secret_name: str) -> dict:
        """
        Auth tier: Tier 2 — Snowflake credentials loaded from Azure Key Vault.

        The KV secret is a JSON blob with keys: account, user, auth_type,
        token_or_key, (optional) private_key_passphrase.
        auth_type ∈ {"oauth", "private_key"}.
        """
        import json
        raw = self.get_secret(account_secret_name)
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            raise RuntimeError(f"Snowflake secret '{account_secret_name}' is not valid JSON") from e
        for k in ("account", "user", "auth_type", "token_or_key"):
            if k not in data:
                raise RuntimeError(f"Snowflake secret '{account_secret_name}' missing '{k}'")
        return data
