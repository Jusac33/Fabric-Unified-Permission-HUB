# Fabric Unified Permission Hub — Copilot Instructions

## Project Overview

This is the **Fabric Unified Permission Hub (UPH)** — a FastAPI web application that provides a unified view of permissions across Microsoft Fabric workspaces and Databricks Unity Catalog. It enables comparing, syncing, and auditing permission configurations between these two platforms.

## Tech Stack

- **Backend**: Python 3.11+, FastAPI, Uvicorn, Pydantic v2, Pydantic-Settings
- **Templating**: Jinja2 (server-rendered HTML with HTMX partials)
- **HTTP Client**: `httpx` with persistent connection pools
- **Auth**: Azure Identity (ChainedTokenCredential — ManagedIdentityCredential + AzureCliCredential)
- **APIs**: Microsoft Fabric REST API, Databricks Unity Catalog REST API, Microsoft Graph API
- **Config**: YAML configs in `configs/`, `.env` for secrets, `pydantic-settings` for typed config
- **Testing**: pytest (tests live in `permission-sync/tests/`)

## Project Structure

```
app/                    # FastAPI web application
  main.py              # App entry point, router mounting, startup events
  config.py            # Pydantic-Settings configuration
  connectors/          # External data source connectors (Databricks, Dataverse, Snowflake)
  routers/             # FastAPI route handlers (home, pairings, fabric, databricks, etc.)
  services/            # Business logic (REST clients, caching, diff computation, sync)
  static/              # CSS assets
  templates/           # Jinja2 templates organized by feature
configs/               # YAML/JSON configuration files for pairings and data sources
permission-sync/       # Standalone sync engine with its own tests and config
scripts/               # Utility/debug scripts
```

## Key Conventions

### Python Style
- Use `from __future__ import annotations` for modern type hints
- Use `pydantic` models for data validation at API boundaries
- Use `httpx.Client` (sync) for REST calls — NOT `requests`
- Use `ThreadPoolExecutor` for parallelizing independent I/O-bound REST calls
- Use `threading.Lock` for thread-safe caching
- Never use `async def` for route handlers — this app is synchronous FastAPI

### Azure Authentication
- All Azure tokens go through `app/services/azure_identity.py`
- Token caching with 5-minute TTL per scope
- Credential chain: ManagedIdentityCredential → AzureCliCredential → DefaultAzureCredential
- Token scopes: Fabric (`https://api.fabric.microsoft.com/.default`), Databricks (`2ff814a6-3304-4ab8-85cb-cd0e6f879c1d/.default`), Graph (`https://graph.microsoft.com/.default`)

### REST Clients
- Fabric REST: `app/services/fabric_rest.py` — persistent `httpx.Client` with base_url
- Databricks REST: `app/services/databricks_rest.py` — per-workspace connection pools
- Always include `Authorization: Bearer {token}` headers
- Always set timeouts on HTTP calls

### Caching
- In-memory TTL caches for expensive REST responses (5-minute default)
- Cache keys include workspace/catalog identifiers for isolation
- Support `refresh=1` query param to bust caches

### Templates
- Base template: `templates/base.html`
- HTMX partial templates prefixed with `_` (e.g., `_home_data.html`)
- Full page templates load a skeleton, then HTMX fetches the `_data` partial

### Security
- No secrets in source code — use `.env` and `pydantic-settings`
- Validate all user inputs at API boundaries with Pydantic
- Never interpolate user input into URLs or queries without validation
- Token/credential errors should show helpful messages, not stack traces

## Permission Sync — Verified Operational Facts

These were confirmed against the live estate. Do not re-derive them by trial and error.

### ABAC requires a SQL warehouse for provenance
- Unity Catalog ABAC policies are **tag-driven**. Resolving *which* columns carry a
  governed tag requires querying `information_schema`, which needs a SQL warehouse.
- Set `DBX_WAREHOUSE_ID` in `.env`. Without it:
  - ABAC column masks **still apply correctly** to Fabric (discovered via the
    policies API), but arrive labelled `abac_driven=None`, `policy=None` — visually
    indistinguishable from an ordinary column mask.
  - An extra catalog-scope row is skipped with the misleading message
    *"ABAC tag-driven policy has no Fabric OneLake equivalent"*. This does **not**
    mean ABAC failed — verify the concrete table-scope rows before concluding that.
- `get_table` does **not** expose ABAC-driven column masks. Only explicit
  `columns[].mask` and `row_filter` / `effective_row_filters` appear there.

### Never remove these
- **Fabric workspace role assignments** — the paired workspace can have a single
  Admin, which is also the identity the tool authenticates as. Removing it is an
  unrecoverable lockout. Cleanup tooling must preserve them.
- **Databricks system-generated groups** (`_workspace_users_*`, `_workspace_admins_*`)
  — the UC API rejects granting to them (`Cannot grant privileges on catalog to
  system generated group`), so revoking is **irreversible**.
- `account users` is revocable and restorable, but the sync cannot recreate it
  (no Entra identity), so it is preserved unless explicitly requested.

### Sync semantics
- A sync propagates only its **source-only** bucket (`dbx_only` for `dbx_to_fabric`,
  `fabric_only` for `fabric_to_dbx`). It is a differ, not a backup — clearing both
  sides leaves nothing to reconcile.
- Unity Catalog keys principals by **UPN/email**. Fabric supplies **display names**,
  which must be resolved via Graph before granting or UC returns
  `PRINCIPAL_DOES_NOT_EXIST`.
- UC owners retain control independently of grants, so revoking grants is
  recoverable; ownership is not carried by the sync.
- A full round trip is **not** privilege-neutral — it can broaden scope (a
  table-scoped Fabric role can land as a catalog-level UC grant). Diff against a
  backup after round-tripping.

### Before any destructive permission change
1. `python scripts/backup_permissions.py <pairing_id>` — writes a restore point to `audits/`.
2. `python scripts/permission_reset.py cleanup <pairing_id> [--side=fabric|dbx]` — dry-run first.
3. Restore with `python scripts/permission_reset.py restore <backup.json> --apply`,
   which replays the backup directly and does not depend on the sync engine.

### Verification discipline
Do not trust the app's own audit log as proof. `ok=N` only means the API call
returned success. Confirm by reading back from the Fabric and Databricks APIs
independently, and check the **contents** of what was created (members, paths,
actions, `decisionRules[].constraints`) — not just the object count.

## Running the App

```powershell
.\.venv\Scripts\Activate.ps1
uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

Use `--reload` when iterating: a server started without it keeps stale bytecode,
which silently invalidates any fix you make to `app/services/*`.

## Testing

```powershell
cd permission-sync
pytest -v
```
