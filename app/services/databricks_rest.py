"""Direct Databricks Unity Catalog client using the user's az identity.

Databricks accepts Azure AD tokens issued for resource
2ff814a6-3304-4ab8-85cb-cd0e6f879c1d (the Azure Databricks login app).
This means we can read UC metadata + grants with just `az login` — no PAT,
no service principal secret, no DBX_ACCOUNT_API_TOKEN required.
"""
from __future__ import annotations
import threading
import time
from typing import List, Optional, Iterable
import httpx
from app.services.azure_identity import get_token
from app.validation import normalize_databricks_workspace_url

# Azure Databricks resource ID — same on every Azure tenant
DBX_RESOURCE_ID = "2ff814a6-3304-4ab8-85cb-cd0e6f879c1d"

_TRANSIENT = (httpx.ConnectError, httpx.ReadError, httpx.RemoteProtocolError,
              httpx.ConnectTimeout, httpx.ReadTimeout)

# Token cache — avoids calling get_token() (which shells out to az CLI) on every request.
_token_lock = threading.Lock()
_cached_dbx_token: str | None = None
_cached_dbx_token_at: float = 0.0
_TOKEN_TTL = 300.0  # 5 min (Azure tokens live ~60-75 min; refresh well before)


def _dbx_token() -> str:
    global _cached_dbx_token, _cached_dbx_token_at
    with _token_lock:
        if _cached_dbx_token and (time.monotonic() - _cached_dbx_token_at) < _TOKEN_TTL:
            return _cached_dbx_token
    token = get_token(f"{DBX_RESOURCE_ID}/.default")
    with _token_lock:
        _cached_dbx_token = token
        _cached_dbx_token_at = time.monotonic()
    return token


# Per-workspace connection pool cache
_pool_lock = threading.Lock()
_pools: dict[str, httpx.Client] = {}


def _get_pool(workspace_url: str) -> httpx.Client:
    with _pool_lock:
        if workspace_url not in _pools:
            _pools[workspace_url] = httpx.Client(
                base_url=workspace_url,
                timeout=httpx.Timeout(60, connect=10),
                limits=httpx.Limits(max_connections=30, max_keepalive_connections=15),
            )
        return _pools[workspace_url]


class DatabricksUCClient:
    def __init__(self, workspace_url: str):
        normalized = normalize_databricks_workspace_url(workspace_url)
        if not normalized:
            raise ValueError("Databricks workspace URL must be an Azure Databricks HTTPS host.")
        self.workspace_url = normalized
        self._client = _get_pool(self.workspace_url)

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {_dbx_token()}", "Content-Type": "application/json"}

    def _get(self, path: str, params: Optional[dict] = None) -> dict:
        last = None
        for i in range(4):
            try:
                r = self._client.get(path, headers=self._headers(),
                              params=params)
                r.raise_for_status()
                return r.json()
            except _TRANSIENT as e:
                last = e
                time.sleep(1 + i)
        raise last

    def _patch(self, path: str, json_body: dict) -> dict:
        last = None
        for i in range(4):
            try:
                r = self._client.patch(path, headers=self._headers(),
                                json=json_body)
                r.raise_for_status()
                return r.json() if r.text else {}
            except _TRANSIENT as e:
                last = e
                time.sleep(1 + i)
        raise last

    def _post(self, path: str, json_body: dict | None = None) -> dict:
        last = None
        for i in range(4):
            try:
                r = self._client.post(path, headers=self._headers(),
                                      json=json_body or {})
                r.raise_for_status()
                return r.json() if r.text else {}
            except _TRANSIENT as e:
                last = e
                time.sleep(1 + i)
        raise last

    # --- Unity Catalog ---
    def list_catalogs(self) -> List[dict]:
        return self._get("/api/2.1/unity-catalog/catalogs").get("catalogs", []) or []

    def list_schemas(self, catalog: str) -> List[dict]:
        return self._get("/api/2.1/unity-catalog/schemas",
                         params={"catalog_name": catalog}).get("schemas", []) or []

    def list_tables(self, catalog: str, schema: str) -> List[dict]:
        return self._get("/api/2.1/unity-catalog/tables",
                         params={"catalog_name": catalog, "schema_name": schema,
                                 "max_results": 1000}).get("tables", []) or []

    def get_table(self, full_name: str) -> dict:
        """Return table metadata including row filters and column masks when present."""
        return self._get(f"/api/2.1/unity-catalog/tables/{full_name}")

    def get_function(self, full_name: str) -> dict:
        """Return UC function metadata including SQL routine definition when available."""
        return self._get(f"/api/2.1/unity-catalog/functions/{full_name}")

    def get_grants(self, securable_type: str, full_name: str) -> List[dict]:
        """securable_type ∈ {catalog, schema, table}; full_name uses dotted form."""
        try:
            return self._get(
                f"/api/2.1/unity-catalog/permissions/{securable_type}/{full_name}"
            ).get("privilege_assignments", []) or []
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return []
            raise

    def update_grants(self, securable_type: str, full_name: str,
                      changes: List[dict]) -> dict:
        """Update grants. changes = [{"principal": "...", "add": [...], "remove": [...]}]"""
        return self._patch(
            f"/api/2.1/unity-catalog/permissions/{securable_type}/{full_name}",
            {"changes": changes},
        )

    def list_policies(self, securable_type: str, full_name: str,
                      include_inherited: bool = False) -> List[dict]:
        """List Unity Catalog ABAC (attribute-based) policies on a securable.

        securable_type ∈ {catalog, schema, table}; full_name uses dotted form.
        Returns row-filter / column-mask policies that apply dynamically via
        governed tags. Returns [] when ABAC is unavailable on the workspace
        (older runtimes respond 404/400/501) so discovery degrades gracefully.
        """
        path = f"/api/2.1/unity-catalog/policies/{securable_type}/{full_name}"
        out: List[dict] = []
        params: dict = {}
        if include_inherited:
            params["include_inherited"] = "true"
        page_token: Optional[str] = None
        for _ in range(50):  # hard cap to bound pagination
            if page_token:
                params["page_token"] = page_token
            try:
                body = self._get(path, params=dict(params))
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code in (400, 404, 501):
                    return out
                raise
            out.extend(body.get("policies") or [])
            page_token = body.get("next_page_token")
            if not page_token:
                break
        return out

    # --- SQL warehouse statement execution ---
    def ensure_sql_warehouse_running(
        self,
        warehouse_id: str,
        *,
        timeout_seconds: int = 300,
        poll_seconds: int = 5,
    ) -> None:
        """Start a SQL warehouse when needed and wait until it is RUNNING."""
        if not warehouse_id:
            raise ValueError("Databricks SQL warehouse id is required")

        details = self._get(f"/api/2.0/sql/warehouses/{warehouse_id}")
        if details.get("state") == "RUNNING":
            return

        self._post(f"/api/2.0/sql/warehouses/{warehouse_id}/start")
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            time.sleep(poll_seconds)
            details = self._get(f"/api/2.0/sql/warehouses/{warehouse_id}")
            if details.get("state") == "RUNNING":
                return

        raise TimeoutError(
            f"Databricks SQL warehouse {warehouse_id} did not reach RUNNING"
        )

    def execute_sql(
        self,
        warehouse_id: str,
        statement: str,
        *,
        timeout_seconds: int = 300,
        poll_seconds: int = 2,
    ) -> dict:
        """Run a single SQL statement and wait for completion."""
        if not warehouse_id:
            raise ValueError("Databricks SQL warehouse id is required")
        if not statement.strip():
            raise ValueError("SQL statement is required")

        body = {
            "warehouse_id": warehouse_id,
            "statement": statement,
            "wait_timeout": "30s",
            "on_wait_timeout": "CONTINUE",
        }
        response = self._post("/api/2.0/sql/statements", body)
        statement_id = response.get("statement_id")
        status = (response.get("status") or {}).get("state")
        deadline = time.monotonic() + timeout_seconds

        while status in {"PENDING", "RUNNING"}:
            if not statement_id:
                raise RuntimeError("Databricks SQL statement did not return an id")
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"Databricks SQL statement {statement_id} timed out"
                )
            time.sleep(poll_seconds)
            response = self._get(f"/api/2.0/sql/statements/{statement_id}")
            status = (response.get("status") or {}).get("state")

        if status != "SUCCEEDED":
            error = (response.get("status") or {}).get("error") or {}
            message = error.get("message") or str(response)
            raise RuntimeError(f"Databricks SQL failed: {message}")

        return response

    # --- workspace SCIM (users / groups / SPs) ---
    def list_users(self) -> List[dict]:
        return self._get("/api/2.0/preview/scim/v2/Users",
                         params={"count": 200}).get("Resources", []) or []

    def list_groups(self) -> List[dict]:
        return self._get("/api/2.0/preview/scim/v2/Groups",
                         params={"count": 200}).get("Resources", []) or []

    def list_service_principals(self) -> List[dict]:
        return self._get("/api/2.0/preview/scim/v2/ServicePrincipals",
                         params={"count": 200}).get("Resources", []) or []

    # --- convenience ---
    def iter_securables(self, only_catalog: Optional[str] = None) -> Iterable[tuple[str, str]]:
        """Yield (securable_type, full_name) for every catalog/schema/table."""
        cats = [c for c in self.list_catalogs()
                if not only_catalog or c.get("name") == only_catalog]
        for c in cats:
            cn = c["name"]
            yield ("catalog", cn)
            try:
                schemas = self.list_schemas(cn)
            except httpx.HTTPStatusError:
                continue
            for s in schemas:
                sn = s["name"]
                if sn == "information_schema":
                    continue
                yield ("schema", f"{cn}.{sn}")
                try:
                    for t in self.list_tables(cn, sn):
                        yield ("table", f"{cn}.{sn}.{t['name']}")
                except httpx.HTTPStatusError:
                    continue
