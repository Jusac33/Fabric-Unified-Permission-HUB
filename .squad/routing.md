# Work Routing — Fabric UPH

How to decide who handles what.

## Routing Table

| Work Type | Route To | Examples |
|-----------|----------|----------|
| New features & routes | Feature Builder 🔧 | Add page, create endpoint, new connector, add service |
| Debugging & errors | Debug Mode 🐛 | Auth failures, REST errors, template issues, 500s |
| API inspection | API Explorer 🔍 | List workspaces, check permissions, inspect UC grants, resolve users |
| Security review | Security Reviewer 🔒 | OWASP audit, credential review, XSS check, SSRF prevention |
| Performance tuning | Performance Reviewer ⚡ | Connection pooling, caching, parallel I/O, bottleneck diagnosis |
| Testing & coverage | Test Runner 🧪 | Run pytest, write tests, fix failures, drive coverage |
| Fabric workspace ops | Fabric Admin 📦 | Workspace roles, deploy items, fab CLI, capacity monitoring |
| Data engineering | Fabric Data Engineer 🏗️ | Spark notebooks, pipelines, medallion architecture, Delta Lake |
| SQL queries | SQL Query Agent 📊 | T-SQL, warehouse queries, lakehouse SQL endpoints, row counts |
| PySpark analysis | Spark Analyst 🔥 | Livy sessions, DataFrames, cross-lakehouse joins, Delta time-travel |
| DAX & semantic models | Power BI Analyst 📈 | DAX queries, model metadata, measures, relationships |
| BI solution design | Power BI Architect 📐 | Star schema design, spec documents, architecture planning |
| BI implementation | Power BI Developer 💻 | TMDL, PBIR reports, DAX measures, deploy to Fabric |
| KQL & real-time | Eventhouse Analyst 📡 | KQL queries, Eventhouse tables, ingestion, retention policies |
| Session logging | Scribe 📋 | Automatic — never needs routing |

## Module Ownership

| Module | Primary | Secondary |
|--------|---------|-----------|
| `app/routers/` | Feature Builder 🔧 | Debug Mode 🐛 |
| `app/services/` | Feature Builder 🔧 | Performance Reviewer ⚡ |
| `app/connectors/` | Feature Builder 🔧 | API Explorer 🔍 |
| `app/templates/` | Feature Builder 🔧 | Security Reviewer 🔒 |
| `app/services/azure_identity.py` | Debug Mode 🐛 | Security Reviewer 🔒 |
| `app/services/fabric_rest.py` | API Explorer 🔍 | Performance Reviewer ⚡ |
| `app/services/databricks_rest.py` | API Explorer 🔍 | Performance Reviewer ⚡ |
| `permission-sync/` | Test Runner 🧪 | Feature Builder 🔧 |
| `configs/` | Feature Builder 🔧 | — |

## Issue Routing

| Label | Action | Who |
|-------|--------|-----|
| `squad` | Triage: analyze issue, assign `squad:{member}` label | Feature Builder (Lead) |
| `squad:feature-builder` | New feature work | Feature Builder |
| `squad:debug` | Bug investigation | Debug Mode |
| `squad:security` | Security audit | Security Reviewer |
| `squad:test-runner` | Test coverage | Test Runner |
| `squad:performance` | Performance optimization | Performance Reviewer |
| `squad:api-explorer` | API inspection | API Explorer |

## Rules

1. **Eager by default** — spawn all agents who could usefully start work, including anticipatory downstream work.
2. **Scribe always runs** after substantial work, always as `mode: "background"`. Never blocks.
3. **Quick facts → coordinator answers directly.** Don't spawn an agent for "what port does the server run on?"
4. **When two agents could handle it**, pick the one whose domain is the primary concern.
5. **"Team, ..." → fan-out.** Spawn all relevant agents in parallel as `mode: "background"`.
6. **Anticipate downstream work.** If a feature is being built, spawn Test Runner for test cases simultaneously.
7. **Security-sensitive changes** → always include Security Reviewer in parallel.
8. **Performance-sensitive changes** (REST clients, caching, connection pools) → include Performance Reviewer.
9. **Spec-driven Power BI work** → Power BI Architect designs (produces spec), then Power BI Developer implements from spec.
