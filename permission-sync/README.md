# Permission Sync v1

Bidirectional permission reconciliation between **Microsoft Fabric** and external governance sources (**Unity Catalog**, **Snowflake**) using a canonical access-class model.

## Design principles
- **Zero hardcoded IDs** — every workspace/catalog/account comes from `config/settings.yaml` or is discovered at runtime.
- **MI-first auth** — `ManagedIdentityCredential` in Azure, `DefaultAzureCredential` locally (`az login`). No client secrets in code.
- **Canonical permission model** — every source maps to one of seven access classes (`DATA_READ`, `DATA_WRITE`, `DATA_ADMIN`, `OBJECT_USE`, `WORKSPACE_VIEW`, `WORKSPACE_EDIT`, `WORKSPACE_ADMIN`) via `config/privilege_matrix.yaml`.
- **Reconcile-first** — compare source vs. target, emit a diff, resolve conflicts, apply via writers. Event accelerators are feature-gated and off by default.
- **Three Fabric security layers handled separately** — workspace role, item role, OneLake DAR. A single planner (`FabricWritePlanner`) decides which layer to write for each access class.

## Run locally

```powershell
cd permission-sync
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env          # edit values
az login
python -m src.orchestrator.sync_engine --config config/settings.yaml --mode full_reconcile
```

`dry_run` defaults to `true` — no writes hit Fabric/UC/Snowflake until you set `DRY_RUN=false` (env) or change `sync.dry_run` in the config.

## CLI

```
python -m src.orchestrator.sync_engine [--config PATH] [--mode full_reconcile|event_accelerator]
```

Environment overrides (highest precedence):
- `DRY_RUN=true|false`
- `SYNC_DIRECTION=source_to_fabric|fabric_to_source|bidirectional`

## Layout

```
permission-sync/
├── config/
│   ├── privilege_matrix.yaml     # per-platform privilege ↔ access class tables
│   └── settings.yaml             # runtime config (workspaces, KV secret refs, flags)
├── src/
│   ├── auth/identity.py          # IdentityProvider (MI/DefaultAzureCredential + KV)
│   ├── config/config_loader.py   # resolves kv:// and env:// in YAML
│   ├── model/canonical_permission.py
│   ├── translation/
│   │   ├── privilege_normalizer.py
│   │   └── identity_resolver.py  # Graph API lookups → Entra object ID
│   ├── discovery/
│   │   ├── fabric_discovery.py
│   │   ├── unity_catalog_discovery.py
│   │   └── snowflake_discovery.py
│   ├── reconciler/
│   │   ├── diff_engine.py
│   │   └── conflict_resolver.py
│   ├── writers/
│   │   ├── fabric_write_planner.py
│   │   ├── fabric_writer.py
│   │   ├── unity_catalog_writer.py
│   │   └── snowflake_writer.py
│   ├── policy_weaver/policy_weaver_client.py
│   ├── onesync/                  # optional OneSync Permissions API integration
│   ├── observability/
│   │   ├── audit_logger.py
│   │   └── metrics_emitter.py
│   └── orchestrator/sync_engine.py
├── tests/
└── .github/workflows/permission-sync.yml
```

## Security / Auth tiers

| Tier | Mechanism | Used by |
|------|-----------|---------|
| 1 | Managed Identity → AAD token | Fabric REST, Unity Catalog REST, Graph API, Blob audit |
| 2 | KV secret (via Tier-1 MI) | Snowflake user/pwd, OneSync API key |

No client secret ever appears in code or config; Tier-2 secrets are fetched at runtime from Key Vault using the process's managed identity.

## Tests

```
pytest -q
```

## CI

See [.github/workflows/permission-sync.yml](.github/workflows/permission-sync.yml). Uses OIDC federated credential — no client secret stored in GitHub.
