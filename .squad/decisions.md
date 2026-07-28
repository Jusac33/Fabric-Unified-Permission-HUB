# Decisions

> Team decisions that all agents must respect. Managed by Scribe.

---

## Foundational Directives

### Synchronous FastAPI — no async
**By:** maintainer
**What:** All route handlers use `def`, never `async def`. This app is synchronous FastAPI.
**Why:** Consistent with httpx sync client and ThreadPoolExecutor patterns.

### httpx only — no requests library
**By:** maintainer
**What:** Use `httpx.Client` for all HTTP calls. Never use `requests`.
**Why:** Persistent connection pools, timeout enforcement, modern API.

### Azure Identity chain order
**By:** maintainer
**What:** ManagedIdentityCredential → AzureCliCredential → DefaultAzureCredential. Token cache 5-min TTL per scope.
**Why:** MSI in production, CLI for local dev, Default as fallback.

### No secrets in code
**By:** maintainer
**What:** All secrets via `.env` and `pydantic-settings`. Never commit credentials.
**Why:** Security baseline — OWASP compliance.

### HTMX partial pattern
**By:** maintainer
**What:** Full pages load a skeleton, then HTMX fetches `_data` partials. Partial templates prefixed with `_`.
**Why:** Fast perceived load, progressive enhancement.

### Spec-driven Power BI workflow
**By:** maintainer
**What:** Power BI Architect designs and writes `specs/*.spec.md`. Power BI Developer implements from specs. Never skip the spec.
**Why:** Architecture decisions before code prevents rework.

### Parallel REST calls
**By:** maintainer
**What:** Use `ThreadPoolExecutor` for independent REST calls. Never serialize calls that can run in parallel.
**Why:** Performance — reduced latency from 25s to 2.3s cold, 1.2s cached.

## Governance

- All meaningful changes require team consensus
- Document architectural decisions here
- Keep history focused on work, decisions focused on direction
