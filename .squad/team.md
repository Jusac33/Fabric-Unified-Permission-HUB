# Fabric UPH — Squad Team

> Unified view of permissions across Microsoft Fabric workspaces and Databricks Unity Catalog.
> *"One hub to see them all."*

## Coordinator

| Name | Role | Notes |
|------|------|-------|
| Squad | Coordinator | Routes work, enforces handoffs and reviewer gates. Does not generate domain artifacts. |

## Members

| Name | Role | Charter | Status |
|------|------|---------|--------|
| Feature Builder | App Developer | `.squad/agents/feature-builder/charter.md` | ✅ Active |
| Debug Mode | Debugger | `.squad/agents/debug/charter.md` | ✅ Active |
| API Explorer | API Inspector | `.squad/agents/api-explorer/charter.md` | ✅ Active |
| Security Reviewer | Security | `.squad/agents/security-reviewer/charter.md` | ✅ Active |
| Performance Reviewer | Performance | `.squad/agents/performance-reviewer/charter.md` | ✅ Active |
| Test Runner | Quality & Tests | `.squad/agents/test-runner/charter.md` | ✅ Active |
| Fabric Admin | Fabric Ops | `.squad/agents/fabric-admin/charter.md` | ✅ Active |
| Fabric Data Engineer | Data Engineering | `.squad/agents/fabric-data-engineer/charter.md` | ✅ Active |
| SQL Query Agent | SQL Analytics | `.squad/agents/sql-query/charter.md` | ✅ Active |
| Spark Analyst | Spark Analytics | `.squad/agents/spark-analyst/charter.md` | ✅ Active |
| Power BI Analyst | BI Analytics | `.squad/agents/powerbi-analyst/charter.md` | ✅ Active |
| Power BI Architect | BI Design | `.squad/agents/powerbi-architect/charter.md` | ✅ Active |
| Power BI Developer | BI Implementation | `.squad/agents/powerbi-developer/charter.md` | ✅ Active |
| Eventhouse Analyst | Real-Time Analytics | `.squad/agents/eventhouse-analyst/charter.md` | ✅ Active |
| Scribe | Session Logger | `.squad/agents/scribe/charter.md` | 📋 Silent |
| Ralph | Work Monitor | `.squad/agents/ralph/charter.md` | 🔄 Monitor |

## Project Context

 - **Owner:** maintainer
- **Stack:** Python 3.11+, FastAPI, Uvicorn, Jinja2, HTMX, httpx, Azure Identity, Pydantic v2
- **Description:** Web application providing unified view of permissions across Microsoft Fabric workspaces and Databricks Unity Catalog — comparing, syncing, and auditing permission configurations
- **APIs:** Microsoft Fabric REST API, Databricks Unity Catalog REST API, Microsoft Graph API
- **Auth:** Azure ChainedTokenCredential (ManagedIdentity → AzureCLI → Default)
- **Distribution:** Local development (`uvicorn app.main:app --reload`)
- **Created:** 2026-04-21
