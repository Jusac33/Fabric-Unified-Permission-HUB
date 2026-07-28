"""Snowflake discovery via SHOW GRANTS (real-time, not ACCOUNT_USAGE)."""
from __future__ import annotations
import logging
from typing import List, Optional
from uuid import uuid4

from src.auth.identity import IdentityProvider
from src.model.canonical_permission import CanonicalPermission
from src.translation.privilege_normalizer import PrivilegeNormalizer
from src.translation.identity_resolver import IdentityResolver

log = logging.getLogger(__name__)


class SnowflakeDiscovery:
    """Auth tier: Tier 2 — per-account credentials loaded from Key Vault."""

    def __init__(self, config: dict, identity: IdentityProvider,
                 normalizer: PrivilegeNormalizer, resolver: IdentityResolver,
                 run_id: Optional[str] = None):
        self._cfg = config.get("discovery", {}).get("snowflake", {})
        self._prefix = self._cfg.get("account_secret_prefix", "")
        self._identity = identity
        self._normalizer = normalizer
        self._resolver = resolver
        self._run_id = run_id or uuid4().hex

    def _connect(self, auth_bundle: dict):
        try:
            import snowflake.connector
        except ImportError:
            log.warning("snowflake-connector-python not installed — skipping")
            return None
        kwargs = {
            "account": auth_bundle["account"],
            "user": auth_bundle["user"],
            "database": "SNOWFLAKE",
            "schema": "ACCOUNT_USAGE",
            "disable_ocsp_checks": True,
        }
        if auth_bundle["auth_type"] == "oauth":
            kwargs["authenticator"] = "oauth"
            kwargs["token"] = auth_bundle["token_or_key"]
        elif auth_bundle["auth_type"] == "private_key":
            kwargs["authenticator"] = "SNOWFLAKE_JWT"
            kwargs["private_key"] = auth_bundle["token_or_key"].encode("utf-8")
            if auth_bundle.get("private_key_passphrase"):
                kwargs["private_key_passphrase"] = auth_bundle["private_key_passphrase"]
        return snowflake.connector.connect(**kwargs)

    def discover_all(self) -> List[CanonicalPermission]:
        out: List[CanonicalPermission] = []
        if not self._prefix:
            log.info("Snowflake: no account_secret_prefix; skipping")
            return out
        try:
            accounts = self._identity.list_secret_names(self._prefix)
        except Exception as e:
            log.warning("Snowflake: cannot enumerate KV secrets: %s", e)
            return out
        log.info("Snowflake: %d account secret(s) found", len(accounts))
        for secret_name in accounts:
            try:
                bundle = self._identity.get_snowflake_auth(secret_name)
                out.extend(self._discover_account(bundle))
            except Exception as e:
                log.warning("Snowflake account '%s' failed: %s", secret_name, e)
        log.info("Snowflake: %d canonical permissions", len(out))
        return out

    def _discover_account(self, auth_bundle: dict) -> List[CanonicalPermission]:
        out: List[CanonicalPermission] = []
        conn = self._connect(auth_bundle)
        if conn is None:
            return out
        try:
            cur = conn.cursor()
            cur.execute("SHOW DATABASES")
            dbs = [r[1] for r in cur.fetchall()]
            for db in dbs:
                cur.execute(f"SHOW GRANTS ON DATABASE {db}")
                out.extend(self._rows_to_canonical(cur, "database", db, auth_bundle["account"]))
                cur.execute(f"SHOW SCHEMAS IN DATABASE {db}")
                schemas = [r[1] for r in cur.fetchall()]
                for sch in schemas:
                    full_s = f"{db}.{sch}"
                    cur.execute(f"SHOW GRANTS ON SCHEMA {full_s}")
                    out.extend(self._rows_to_canonical(cur, "schema", full_s,
                                                      auth_bundle["account"]))
                    try:
                        cur.execute(f"SHOW TABLES IN SCHEMA {full_s}")
                        tbls = [r[1] for r in cur.fetchall()]
                    except Exception:
                        tbls = []
                    for tb in tbls:
                        full_t = f"{full_s}.{tb}"
                        try:
                            cur.execute(f"SHOW GRANTS ON TABLE {full_t}")
                            out.extend(self._rows_to_canonical(
                                cur, "table", full_t, auth_bundle["account"]))
                        except Exception as e:
                            log.warning("SHOW GRANTS %s failed: %s", full_t, e)
        finally:
            conn.close()
        return out

    def _rows_to_canonical(self, cur, securable_type: str, fqn: str,
                           account: str) -> List[CanonicalPermission]:
        # SHOW GRANTS columns (positional): created_on, privilege, granted_on,
        #   name, granted_to, grantee_name, grant_option, granted_by
        out: List[CanonicalPermission] = []
        for row in cur.fetchall():
            privilege = row[1] if len(row) > 1 else ""
            grantee_name = row[5] if len(row) > 5 else ""
            ident = self._resolver.resolve_to_entra_id(grantee_name, "snowflake")
            if not ident:
                continue
            access = self._normalizer.normalize(privilege, "snowflake")
            if not access:
                continue
            out.append(CanonicalPermission(
                principal_entra_id=ident.object_id,
                principal_type=ident.principal_type,
                principal_display_name=ident.display_name or grantee_name,
                access_class=access,
                securable_type=securable_type,
                securable_fqn=fqn,
                workspace_id=account,
                source_platform="snowflake",
                source_raw_privilege=privilege,
                source_raw_object={"row": list(row)},
                sync_run_id=self._run_id,
            ))
        return out
