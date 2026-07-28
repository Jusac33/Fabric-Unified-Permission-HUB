# Fabric Unified Permission Hub

A FastAPI-based web app to inspect, compare, and synchronize **data access
policies** between **Databricks Unity Catalog** (plus Snowflake and Dataverse)
and Microsoft Fabric **OneLake Security**. It provides a side-by-side permission
diff per pairing and can apply changes in either direction (dry-run by default).

## Architecture

- **FastAPI + Jinja2 + HTMX + Tailwind (CDN)** — single-process, synchronous
  Python web app
- **Pluggable connector layer** (`app/connectors/`) — wraps the underlying
  policy-weaver plugins behind a unified `Principal / DataObject / Grant` model
- **Direct REST clients** (`app/services/`) — Fabric REST, Databricks Unity
  Catalog REST, and Microsoft Graph, all authenticated with the caller's Azure
  identity (`az login` / managed identity), token-cached per scope
- **Pairing engine** — `pair_diff.py` (normalize + compare), `pair_apply.py`
  (auto-route each change to the correct Fabric/UC layer), `abac_resolver.py`
  (materialize tag-driven ABAC policies onto concrete tables/columns)
- **Routers** (`app/routers/`) — `home`/`pairings`, `databricks`, `fabric`,
  `sources`, `inventory`, `capabilities`, `sync`

```
app/
  main.py
  config.py
  connectors/    base.py, databricks.py, snowflake.py, dataverse.py
  services/      pair_diff.py, pair_apply.py, abac_resolver.py,
                 fine_grained_policy.py, fabric_rest.py, databricks_rest.py,
                 azure_identity.py, inventory_service.py, sync_service.py
  routers/       home.py, pairings.py, databricks.py, fabric.py,
                 sources.py, inventory.py, capabilities.py, sync.py
  templates/     base.html, pairings/, databricks/, fabric/, sources/, sync/
  static/        app.css
configs/         pairings.json, databricks_*.yaml, snowflake_*.yaml, dataverse_*.yaml
```

## Setup

```powershell
# from project root
copy .env.example .env
.\run.ps1
```

Open <http://127.0.0.1:8000>.

## Adding a source

1. Drop a YAML config into `configs/`
2. Filename **must** start with `databricks_`, `snowflake_`, or `dataverse_`
   (the prefix tells the hub which connector to use)
3. Refresh the dashboard — it will appear automatically

Sample configs (`*_sample.yaml`) and any config still containing unresolved
`<placeholder>` tokens are treated as **templates**: they are hidden from the
active Sources list (shown in a collapsed "Templates" section) and skipped by
inventory aggregation and the Sync dropdown. Fill in real values to activate one.

See `configs/databricks_sample.yaml`, `configs/snowflake_sample.yaml`,
`configs/dataverse_sample.yaml`.

## Features

- **Pairings** — define a Databricks UC catalog ↔ Fabric workspace pairing, then
  view a side-by-side permission **diff** grouped by scope (catalog / schema /
  table / workspace). Each change is auto-routed to the correct target layer and
  can be applied in either direction with a **dry-run** default.
- **Apply routing** — catalog/workspace scope → Fabric workspace role; table
  read → OneLake Data Access Role; unsupported scopes (e.g. schema-only, or
  write/admin on a read-only mirror) are safely skipped, never broadened.
- **Fine-grained security (RLS/CLS)** — Unity Catalog row filters and column
  masks are discovered and translated to Fabric OneLake DAS row predicates and
  column-visibility constraints (and the reverse, where it maps cleanly).
- **Attribute-based access control (ABAC)** — Unity Catalog ABAC policies are
  discovered and, when a SQL warehouse is configured (`DBX_WAREHOUSE_ID`),
  **materialized** onto the concrete tables/columns their governed-tag
  conditions match, then converted to OneLake DAS constraints. Tag-driven
  policies have no static Fabric equivalent, so this is a point-in-time snapshot;
  unresolved policies are surfaced for review and skipped on apply. ABAC-driven
  rows are flagged with an **ABAC** badge in the diff view. See
  `docs/permission-model.md`.
- **Sources / detail** — Principals, Objects, Grants normalized across all
  source systems
- **Inventory** — at-a-glance counts per active config
- **Sync** — run policy synchronization (with **dry-run** option) from the UI;
  logs are streamed into the job card and snapshots are persisted under
  `./snapshots/<job-id>/`
- **Apply audit trail** — permission apply and dry-run attempts are written to
  `./audits/permission-applies.jsonl`; keep this folder private because it can
  contain principals, scopes, workspace IDs, and grant decisions.
- **Permission model notes** — see `docs/permission-model.md` for the Fabric ↔
  Databricks Unity Catalog scope, privilege, RLS/CLS, and ABAC mapping
  assumptions.
- **Source Matrix** — `/capabilities` shows the OneSync-style platform map for
  Fabric OneLake, Fabric shortcuts, Databricks UC, Snowflake, and AWS Lake
  Formation/S3. Shortcuts are treated as bridges: source-side permissions must
  be reconciled with the shortcut target authority as well as OneLake DAS.

## Platform features

These turn the hub from a manual tool into a monitorable control plane. See
`docs/roadmap.md` for details.

- **Persistence** — SQLite store at `DATA_DIR/uph.db` (pairings, sync jobs, diff
  snapshots, audit, identity queue, approvals). Legacy `configs/pairings.json`
  is imported once on first run.
- **Drift detection** — every diff records a snapshot; the diff view highlights
  rows added/removed since the previous check.
- **Scheduled re-scan** — set `SCAN_INTERVAL_MINUTES > 0` to periodically re-diff
  all pairings and record drift in the background.
- **Operations console** (`/operations`) — queryable audit trail, identity
  reconciliation queue, and approval requests.
- **Approval workflow** — set `REQUIRE_APPROVAL=true` to gate real applies behind
  an approver decision.
- **Authentication** — set `AUTH_ENABLED=true` for Entra SSO with viewer /
  approver / admin roles (email allowlists). Off by default.
- **REST/JSON API + CSV** — `/api/pairings`, `/api/audit`, `/api/audit.csv`,
  `/api/pairings/{id}/drift`, `/api/audit/{id}/rollback-plan`.
- **Rollback plans** — generate the reverse plan for any recorded apply
  (review-only; revoke is performed deliberately by an operator).

## Configuration

Settings are loaded from `.env` via `pydantic-settings` (`app/config.py`). Key
optional values:

- `DBX_WORKSPACE_URL` — default Databricks workspace URL for the UI
- `DBX_WAREHOUSE_ID` — SQL warehouse used to read governed tags for ABAC
  materialization and to apply Fabric→UC RLS/CLS; without it, ABAC policies are
  surfaced as review-only
- `FABRIC_WORKSPACE_ID` — default Fabric workspace pointer for the UI

No secrets are hardcoded; authentication uses the caller's Azure identity.

## Support & Community

- **Issues:** File bugs and feature requests on this repo.
- **Discussions:** Ask questions in the Discussions tab.

## License

Copyright (c) Microsoft Corporation. All rights reserved.

Licensed under the MIT License. See [LICENSE](LICENSE) for the full text.

## Trademarks

This project may contain trademarks or logos for projects, products, or
services. Authorized use of Microsoft trademarks or logos is subject to and must
follow [Microsoft's Trademark & Brand Guidelines](https://www.microsoft.com/legal/intellectualproperty/trademarks/usage/general).
Use of Microsoft trademarks or logos in modified versions of this project must
not cause confusion or imply Microsoft sponsorship. Any use of third-party
trademarks or logos are subject to those third-party's policies.

## Disclaimer

This presentation, demonstration, and demonstration model are for informational
purposes only and (1) are not subject to SOC 1 and SOC 2 compliance audits, and
(2) are not designed, intended or made available as a medical device(s) or as a
substitute for professional medical advice, diagnosis, treatment or judgment.
Microsoft makes no warranties, express or implied, in this presentation,
demonstration, and demonstration model. Nothing in this presentation,
demonstration, or demonstration model modifies any of the terms and conditions
of Microsoft's written and signed agreements. This is not an offer and
applicable terms and the information provided are subject to revision and may be
changed at any time by Microsoft.

This presentation, demonstration, and demonstration model do not give you or
your organization any license to any patents, trademarks, copyrights, or other
intellectual property covering the subject matter in this presentation,
demonstration, and demonstration model.

The information contained in this presentation, demonstration and demonstration
model represents the current view of Microsoft on the issues discussed as of the
date of presentation and/or demonstration, for the duration of your access to
the demonstration model. Because Microsoft must respond to changing market
conditions, it should not be interpreted to be a commitment on the part of
Microsoft, and Microsoft cannot guarantee the accuracy of any information
presented after the date of presentation and/or demonstration and for the
duration of your access to the demonstration model.

No Microsoft technology, nor any of its component technologies, including the
demonstration model, is intended or made available as a substitute for the
professional advice, opinion, or judgment of (1) a certified financial services
professional, or (2) a certified medical professional. Partners or customers are
responsible for ensuring the regulatory compliance of any solution they build
using Microsoft technologies.

**DISCLAIMER:** The information contained in this repository and any
accompanying materials (including, but not limited to, scripts, sample codes,
etc.) are provided "AS-IS" and "WITH ALL FAULTS." Any estimated pricing
information is provided solely for demonstration purposes and does not represent
final pricing and Microsoft assumes no liability arising from your use of the
information. Microsoft makes NO GUARANTEES OR WARRANTIES OF ANY KIND, WHETHER
EXPRESSED OR IMPLIED, in providing this information, including any pricing
information.
