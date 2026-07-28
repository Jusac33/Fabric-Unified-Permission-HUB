"""Canonical permission model — the ONLY intermediate representation."""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict


# Canonical access classes
DATA_READ = "DATA_READ"
DATA_WRITE = "DATA_WRITE"
DATA_ADMIN = "DATA_ADMIN"
OBJECT_USE = "OBJECT_USE"
WORKSPACE_VIEW = "WORKSPACE_VIEW"
WORKSPACE_EDIT = "WORKSPACE_EDIT"
WORKSPACE_ADMIN = "WORKSPACE_ADMIN"

ACCESS_CLASSES = frozenset({
    DATA_READ, DATA_WRITE, DATA_ADMIN, OBJECT_USE,
    WORKSPACE_VIEW, WORKSPACE_EDIT, WORKSPACE_ADMIN,
})


@dataclass
class CanonicalPermission:
    # Identity
    principal_entra_id: str
    principal_type: str                # User | Group | ServicePrincipal
    principal_display_name: str = ""

    # Access class
    access_class: str = ""

    # Securable
    securable_type: str = ""           # table|schema|database|catalog|workspace|lakehouse
    securable_fqn: str = ""
    workspace_id: str = ""

    # Provenance
    source_platform: str = ""          # unity_catalog|snowflake|fabric
    source_raw_privilege: str = ""
    source_raw_object: Dict[str, Any] = field(default_factory=dict)

    # Lifecycle
    sync_run_id: str = ""
    discovered_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    is_inherited: bool = False

    def key(self) -> tuple[str, str, str, str]:
        """Equality key used by diff engine."""
        return (
            self.principal_entra_id,
            self.access_class,
            self.securable_fqn,
            self.securable_type,
        )
