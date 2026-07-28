"""Resolve kv:// and env:// references inside YAML config at load time."""
from __future__ import annotations
import logging
import os
import re
from pathlib import Path
from typing import Any

import yaml

from src.auth.identity import IdentityProvider

log = logging.getLogger(__name__)

_KV_PREFIX = "kv://"
_ENV_PREFIX = "env://"
_SECRET_KEY_RE = re.compile(r"_secret$")


def _resolve_value(value: Any, identity: IdentityProvider) -> Any:
    if isinstance(value, str):
        if value.startswith(_KV_PREFIX):
            return identity.get_secret(value[len(_KV_PREFIX):])
        if value.startswith(_ENV_PREFIX):
            return os.environ.get(value[len(_ENV_PREFIX):], "")
    return value


def _walk(obj: Any, identity: IdentityProvider) -> Any:
    if isinstance(obj, dict):
        return {k: _walk(v, identity) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_walk(v, identity) for v in obj]
    return _resolve_value(obj, identity)


def load_config(path: str | Path, identity: IdentityProvider | None = None) -> dict:
    """Load YAML config and resolve kv://, env:// references."""
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"Config file not found: {p}")
    with p.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    identity = identity or IdentityProvider()
    # Apply env:// always; defer kv:// until a secret is actually requested
    # by walking the tree and resolving inline. (For full resolution we simply
    # walk; individual secret fetches are cached by IdentityProvider.)
    try:
        resolved = _walk(raw, identity)
    except RuntimeError as e:
        log.warning("Config partial-resolve: %s (leaving kv:// placeholders in place)", e)
        resolved = raw
    # Apply DRY_RUN and SYNC_DIRECTION env overrides (bootstrap-only)
    if (dr := os.getenv("DRY_RUN")) is not None:
        resolved.setdefault("sync", {})["dry_run"] = dr.lower() == "true"
    if (sd := os.getenv("SYNC_DIRECTION")):
        resolved.setdefault("sync", {})["direction"] = sd
    return resolved
