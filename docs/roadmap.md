# Roadmap & Platform Architecture

This document tracks the platform-maturity features of the Permission Hub and
what remains. It complements `docs/permission-model.md` (the Fabric ↔ UC mapping
semantics).

## Delivered

### Persistence (keystone)
- SQLite store at `DATA_DIR/uph.db` (stdlib `sqlite3`, WAL mode, thread-local
  connections) — `app/services/db.py`.
- Tables: `pairings`, `sync_jobs`, `diff_snapshots`, `audit_events`,
  `identity_queue`, `approvals`.
- Legacy `configs/pairings.json` is imported once on first init.
- Pairings and sync jobs survive restarts; tests are isolated via a per-test
  temp DB fixture (`tests/conftest.py`).

### Drift detection
- Every diff view records a snapshot and reports rows added/removed since the
  previous snapshot — `app/services/drift_service.py`. Surfaced as a banner in
  the diff UI.

### Scheduled re-scan
- Optional background daemon (`SCAN_INTERVAL_MINUTES > 0`) re-diffs every pairing
  and records drift snapshots — `app/services/scheduler.py`. Off by default; no
  external scheduler dependency.

### Authentication & roles
- Entra SSO via MSAL with a stdlib HMAC-signed session cookie (no `itsdangerous`
  dependency) — `app/services/auth.py`, `app/routers/auth.py`.
- Disabled by default (`AUTH_ENABLED=false`); when on, unauthenticated requests
  redirect to `/auth/login`. Email allowlists map to `viewer` / `approver` /
  `admin` roles.

### Approval workflow
- When `REQUIRE_APPROVAL=true`, a real apply is parked as a pending approval
  instead of executing; an approver approves before it runs —
  `app/services/approvals.py`, surfaced on `/operations`.

### Identity reconciliation queue
- Principals that can't be mapped to Entra during apply are parked for an
  operator to resolve/ignore — `app/services/identity_queue.py`.

### Queryable audit + observability
- Audit events are mirrored into the DB (`audit_events`) in addition to the
  append-only JSONL — `app/services/audit_log.py`. Browsable on `/operations`.

### REST/JSON API + CSV export
- `/api/pairings`, `/api/audit`, `/api/audit.csv`, `/api/pairings/{id}/drift`,
  `/api/audit/{id}/rollback-plan` — `app/routers/operations.py`.

### Rollback plan generation
- For any recorded apply, generates the reverse plan (what to revoke) for
  review — `app/services/rollback_service.py`. Read-only by design.

## Next / future

- **Automated rollback execution** — implement revoke APIs for workspace roles,
  OneLake DAR/DAS, and UC grants, then execute a reverse plan transactionally.
  Currently plan-only to protect production permission surfaces.
- **Graph-based group expansion** — resolve nested/SCIM groups in the identity
  queue automatically rather than manual resolution.
- **Fabric → UC ABAC** — only UC → Fabric ABAC materialization exists today.
- **Deeper Snowflake / Dataverse parity** with the UC ↔ Fabric path.
- **Notifications** — email/Teams alerts on drift or pending approvals
  (the Activator path is available in the Fabric skill set).
- **Postgres backend** option for multi-instance deployments.
- **Coverage gate + live integration tests** against mocked Databricks/Fabric.

## Configuration summary

| Setting | Default | Purpose |
| --- | --- | --- |
| `DATA_DIR` | `./data` | SQLite DB location |
| `DBX_WAREHOUSE_ID` | — | Read governed tags for ABAC materialization |
| `SCAN_INTERVAL_MINUTES` | `0` | Background re-scan cadence (0 = off) |
| `REQUIRE_APPROVAL` | `false` | Gate real applies behind approval |
| `AUTH_ENABLED` | `false` | Require Entra SSO |
| `AUTH_ADMIN_EMAILS` / `AUTH_APPROVER_EMAILS` | — | Role allowlists |
