"""Diff two canonical permission sets."""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List

from src.model.canonical_permission import CanonicalPermission


@dataclass
class PermissionConflict:
    conflict_id: str
    principal_entra_id: str
    securable_fqn: str
    securable_type: str
    source_access_class: str
    target_access_class: str
    source_platform: str
    target_platform: str
    detected_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    source_permission: CanonicalPermission = None  # type: ignore
    target_permission: CanonicalPermission = None  # type: ignore


@dataclass
class PermissionDiff:
    to_grant: List[CanonicalPermission] = field(default_factory=list)
    to_revoke: List[CanonicalPermission] = field(default_factory=list)
    conflicts: List[PermissionConflict] = field(default_factory=list)
    unchanged: List[CanonicalPermission] = field(default_factory=list)


def compute_diff(
    current_state: List[CanonicalPermission],
    desired_state: List[CanonicalPermission],
) -> PermissionDiff:
    """
    Equality key: (principal_entra_id, access_class, securable_fqn, securable_type).
    Conflicts: same (principal_entra_id, securable_fqn, securable_type) but
    different access_class.
    """
    from uuid import uuid4

    current_by_key: Dict[tuple, CanonicalPermission] = {p.key(): p for p in current_state}
    desired_by_key: Dict[tuple, CanonicalPermission] = {p.key(): p for p in desired_state}

    diff = PermissionDiff()
    for k, p in desired_by_key.items():
        if k in current_by_key:
            diff.unchanged.append(p)
        else:
            diff.to_grant.append(p)
    for k, p in current_by_key.items():
        if k not in desired_by_key:
            diff.to_revoke.append(p)

    # Conflict detection
    def secure_key(p: CanonicalPermission) -> tuple:
        return (p.principal_entra_id, p.securable_fqn, p.securable_type)

    current_by_sec: Dict[tuple, CanonicalPermission] = {}
    for p in current_state:
        current_by_sec[secure_key(p)] = p
    for p in desired_state:
        k = secure_key(p)
        if k in current_by_sec and current_by_sec[k].access_class != p.access_class:
            diff.conflicts.append(PermissionConflict(
                conflict_id=uuid4().hex,
                principal_entra_id=p.principal_entra_id,
                securable_fqn=p.securable_fqn,
                securable_type=p.securable_type,
                source_access_class=p.access_class,
                target_access_class=current_by_sec[k].access_class,
                source_platform=p.source_platform,
                target_platform=current_by_sec[k].source_platform,
                source_permission=p,
                target_permission=current_by_sec[k],
            ))
    return diff
