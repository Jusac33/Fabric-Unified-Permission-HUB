"""Snowflake grant/revoke — parameterized SQL, no string concatenation of identifiers."""
from __future__ import annotations
import logging
import re
from typing import Optional

from src.model.canonical_permission import CanonicalPermission

log = logging.getLogger(__name__)

_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_\.\"]*$")


def _safe_ident(val: str) -> str:
    """Reject values that don't look like valid Snowflake identifiers."""
    if not _IDENT_RE.match(val):
        raise ValueError(f"Unsafe Snowflake identifier: {val!r}")
    return val


class SnowflakeWriter:
    """Auth tier: Tier 2 — credentials from Key Vault per account."""

    def __init__(self, identity):
        self._identity = identity

    def _connect(self, conn_params: dict):
        try:
            import snowflake.connector
        except ImportError:
            log.warning("snowflake-connector-python not installed")
            return None
        return snowflake.connector.connect(**conn_params)

    def _snowflake_name_for(self, permission: CanonicalPermission) -> Optional[str]:
        """Reverse-resolve canonical Entra ID → Snowflake role/user name via raw object."""
        raw = permission.source_raw_object or {}
        if isinstance(raw, dict):
            row = raw.get("row")
            if row and len(row) > 5:
                return row[5]
        return None

    def _grantee_type(self, permission: CanonicalPermission) -> str:
        return "ROLE" if permission.principal_type == "Group" else "USER"

    def _execute(self, conn_params: dict, permission: CanonicalPermission,
                 target_privilege: str, grant: bool) -> dict:
        sf_name = self._snowflake_name_for(permission)
        if not sf_name:
            return {"action": "skipped",
                    "reason": "no snowflake identity for canonical principal"}
        try:
            sec_type = _safe_ident(permission.securable_type.upper())
            sec_fqn = _safe_ident(permission.securable_fqn)
            priv = _safe_ident(target_privilege)
            grantee = _safe_ident(sf_name)
            grantee_type = self._grantee_type(permission)
        except ValueError as e:
            log.warning("%s", e)
            return {"action": "error", "reason": str(e)}

        verb = "GRANT" if grant else "REVOKE"
        conj = "TO" if grant else "FROM"
        sql = f"{verb} {priv} ON {sec_type} {sec_fqn} {conj} {grantee_type} {grantee}"

        conn = self._connect(conn_params)
        if conn is None:
            return {"action": "error", "reason": "snowflake-connector missing"}
        try:
            cur = conn.cursor()
            cur.execute(sql)
            return {"action": "granted" if grant else "revoked", "sql": sql}
        except Exception as e:
            log.warning("Snowflake %s failed: %s", verb, e)
            return {"action": "error", "sql": sql, "exception": str(e)}
        finally:
            conn.close()

    def grant(self, permission: CanonicalPermission, target_privilege: str,
              connection_params: dict) -> dict:
        return self._execute(connection_params, permission, target_privilege, grant=True)

    def revoke(self, permission: CanonicalPermission, target_privilege: str,
               connection_params: dict) -> dict:
        return self._execute(connection_params, permission, target_privilege, grant=False)
