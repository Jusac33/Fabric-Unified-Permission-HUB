"""Privilege normalizer — YAML-driven, zero-hardcoded privilege strings."""
from __future__ import annotations
import logging
from pathlib import Path
from typing import Dict, Optional

import yaml

log = logging.getLogger(__name__)


class PrivilegeNormalizer:
    def __init__(self, matrix_path: str | Path):
        self._path = Path(matrix_path)
        self._normalize: Dict[str, Dict[str, str]] = {}
        self._denormalize: Dict[str, Dict[str, Optional[str]]] = {}
        self._load()

    def _load(self) -> None:
        if not self._path.is_file():
            raise FileNotFoundError(f"Privilege matrix not found: {self._path}")
        with self._path.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        self._normalize = data.get("normalize", {}) or {}
        self._denormalize = data.get("denormalize", {}) or {}

    def normalize(self, raw_privilege: str, source_platform: str) -> Optional[str]:
        table = self._normalize.get(source_platform) or {}
        key = raw_privilege.strip()
        # Case-insensitive match (Snowflake returns uppercase, Fabric mixed)
        hit = table.get(key)
        if hit is None:
            for k, v in table.items():
                if k.upper() == key.upper():
                    return v
            log.warning("No canonical mapping for %s/%s", source_platform, raw_privilege)
            return None
        return hit

    def denormalize(self, access_class: str, target_platform: str) -> Optional[str]:
        table = self._denormalize.get(target_platform) or {}
        if access_class not in table:
            log.warning("No denormalization entry for %s/%s", target_platform, access_class)
            return None
        val = table[access_class]
        if val is None:
            log.warning("No %s equivalent for canonical class %s — skipping",
                        target_platform, access_class)
            return None
        return val
