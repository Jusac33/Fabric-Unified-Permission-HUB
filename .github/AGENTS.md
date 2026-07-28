# Agents & Skills Orchestration

## Agent Roster

### Project Agents (`.github/agents/`)

| Agent | Role | Backed by Skill |
|-------|------|-----------------|
| **Fabric Admin** | Workspace/item/permission management | `fabric-cli` |
| **Fabric Data Engineer** | Spark, Pipelines, Lakehouse, Medallion | `spark-authoring-cli`, `e2e-medallion-architecture` |
| **SQL Query Agent** | Read-only T-SQL against Warehouse/Lakehouse | `sqldw-consumption-cli` |
| **Spark Analyst** | Interactive PySpark via Livy sessions | `spark-consumption-cli` |
| **Power BI Analyst** | DAX queries, semantic models, reports | `powerbi-*-cli`, `powerbi-*-authoring` |
| **Power BI Architect** | Design specs for PBI solutions (no code) | `powerbi-semantic-model-authoring`, `fabric-cli` |
| **Power BI Developer** | Implement from specs — TMDL, PBIR, DAX, deploy | `powerbi-semantic-model-authoring`, `powerbi-report-authoring`, `fabric-cli` |
| **Eventhouse Analyst** | KQL queries and Eventhouse management | `eventhouse-*-cli` |
| **Security Reviewer** | OWASP Top 10, Azure credential safety | — |
| **Debug Mode** | Systematic FastAPI/Azure debugging | — |
| **Performance Reviewer** | Connection pooling, caching, parallel I/O | — |

## Delegation Rules

When a user request spans multiple domains, the primary agent should delegate to specialized subagents:

- **"query the warehouse"** → SQL Query Agent
- **"analyze with PySpark"** → Spark Analyst
- **"deploy a notebook"** → Fabric Admin
- **"build a medallion lakehouse"** → Fabric Data Engineer
- **"run a DAX query"** → Power BI Analyst
- **"design a semantic model spec"** → Power BI Architect
- **"implement the spec"** → Power BI Developer
- **"create a PBIR report"** → Power BI Developer
- **"query the eventhouse"** → Eventhouse Analyst
- **"check for security issues"** → Security Reviewer
- **"why is this endpoint slow"** → Performance Reviewer
- **"fix this bug"** → Debug Mode

## Spec-Driven Workflow (Power BI)

For complex Power BI projects, use the architect → developer handoff:

1. **User** describes business requirements
2. **Power BI Architect** analyzes data sources, designs star schema, produces `specs/[Name].spec.md`
3. **User** reviews and approves the spec
4. **Power BI Developer** implements from the spec: creates TMDL model, DAX measures, PBIR reports, deploys to Fabric
5. **Power BI Developer** produces `specs/[Name].ExecutionSummary.md`

This separates design from implementation and creates a reviewable checkpoint.

## Skill → Agent Mapping

All 13 installed skills are accessible through agents:

| Skill | Agent |
|-------|-------|
| `fabric-cli` | Fabric Admin, Power BI Architect, Power BI Developer |
| `spark-authoring-cli` | Fabric Data Engineer |
| `spark-consumption-cli` | Spark Analyst |
| `sqldw-authoring-cli` | Fabric Data Engineer |
| `sqldw-consumption-cli` | SQL Query Agent |
| `powerbi-authoring-cli` | Power BI Analyst |
| `powerbi-consumption-cli` | Power BI Analyst |
| `powerbi-report-authoring` | Power BI Analyst, Power BI Developer |
| `powerbi-semantic-model-authoring` | Power BI Analyst, Power BI Architect, Power BI Developer |
| `eventhouse-authoring-cli` | Eventhouse Analyst |
| `eventhouse-consumption-cli` | Eventhouse Analyst |
| `e2e-medallion-architecture` | Fabric Data Engineer |
| `check-updates` | (utility — runs automatically) |
