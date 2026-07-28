"""Azure identity helper — uses az login / managed identity / VS Code login.

policy-weaver itself requires a Service Principal client secret for the *sync*
(apply) path. This module is for read-only operations the hub does on its own
(Fabric REST inventory, OneLake DAP browsing, etc.).
"""
from __future__ import annotations
import os
import time
from typing import Optional
from azure.identity import (
    DefaultAzureCredential,
    AzureCliCredential,
    ManagedIdentityCredential,
    ChainedTokenCredential,
)

# Ensure Azure CLI is on PATH (common issue when VS Code terminals don't inherit PATH)
_AZ_CLI_DIR = r"C:\Program Files\Microsoft SDKs\Azure\CLI2\wbin"
if os.path.isdir(_AZ_CLI_DIR) and _AZ_CLI_DIR not in os.environ.get("PATH", ""):
    os.environ["PATH"] = _AZ_CLI_DIR + os.pathsep + os.environ.get("PATH", "")

_cached_cred = None
_cached_at: float = 0.0
_CACHE_TTL = 120.0  # re-probe every 2 min so az-login recovery works

# Probe scope — use Fabric since that's what the hub actually calls.
_PROBE_SCOPE = "https://api.fabric.microsoft.com/.default"


def _build_chain():
    """Build credential chain. Honours env vars:
      AZURE_USE_MANAGED_IDENTITY=1   → managed identity only
      AZURE_CLIENT_ID=<uami-client-id> → user-assigned managed identity
    Otherwise tries: managed identity → az CLI → DefaultAzureCredential.
    """
    msi_only = os.environ.get("AZURE_USE_MANAGED_IDENTITY", "").lower() in ("1", "true", "yes")
    uami_client_id = os.environ.get("AZURE_CLIENT_ID") or None
    # Only consider MSI when we're plausibly on Azure (env injected by the host)
    on_azure = bool(
        os.environ.get("IDENTITY_ENDPOINT")
        or os.environ.get("MSI_ENDPOINT")
        or os.environ.get("IMDS_ENDPOINT")
        or os.environ.get("AZURE_FEDERATED_TOKEN_FILE")
    )
    creds = []
    if msi_only or on_azure or uami_client_id:
        creds.append(
            ManagedIdentityCredential(client_id=uami_client_id) if uami_client_id else ManagedIdentityCredential()
        )
    if msi_only:
        return creds[0]
    creds.append(AzureCliCredential())
    creds.append(DefaultAzureCredential(exclude_interactive_browser_credential=True))
    return ChainedTokenCredential(*creds)


def get_credential():
    """Re-probes periodically so credential recovery is picked up without a
    server restart. Validates against the Fabric scope so a credential that
    can't actually reach Fabric isn't cached."""
    global _cached_cred, _cached_at
    if _cached_cred is not None and (time.monotonic() - _cached_at) < _CACHE_TTL:
        return _cached_cred
    cred = _build_chain()
    # Validate — raises if no credential in the chain can mint a Fabric token.
    cred.get_token(_PROBE_SCOPE)
    _cached_cred = cred
    _cached_at = time.monotonic()
    return cred


def warm_credential_cache() -> None:
    """Pre-warm the credential + token cache on app startup so the first real
    request doesn't pay the probe penalty."""
    try:
        get_credential()
        # Pre-warm tokens for all scopes the hub uses
        for scope in (
            "https://api.fabric.microsoft.com/.default",
            "2ff814a6-3304-4ab8-85cb-cd0e6f879c1d/.default",
            "https://graph.microsoft.com/.default",
        ):
            try:
                get_token(scope)
            except Exception:
                pass
    except Exception:
        pass  # non-fatal; first real request will retry


def get_token(scope: str) -> str:
    return get_credential().get_token(scope).token


# Per-scope token cache — avoids repeated calls to the credential chain
# (which may shell out to az CLI) for every HTTP request.
_token_cache_lock = __import__("threading").Lock()
_token_cache: dict[str, tuple[str, float]] = {}
_TOKEN_CACHE_TTL = 300.0  # 5 min


def get_token(scope: str) -> str:  # noqa: F811
    with _token_cache_lock:
        entry = _token_cache.get(scope)
        if entry and (time.monotonic() - entry[1]) < _TOKEN_CACHE_TTL:
            return entry[0]
    token = get_credential().get_token(scope).token
    with _token_cache_lock:
        _token_cache[scope] = (token, time.monotonic())
    return token


def get_fabric_token() -> str:
    return get_token("https://api.fabric.microsoft.com/.default")


def get_powerbi_token() -> str:
    return get_token("https://analysis.windows.net/powerbi/api/.default")


def whoami() -> Optional[dict]:
    """Best-effort identity lookup via Microsoft Graph."""
    import httpx
    try:
        token = get_token("https://graph.microsoft.com/.default")
        r = httpx.get(
            "https://graph.microsoft.com/v1.0/me",
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
        if r.status_code == 200:
            return r.json()
    except Exception:
        return None
    return None
