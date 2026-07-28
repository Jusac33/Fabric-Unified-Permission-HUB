---
applyTo: '**/*.py'
description: 'Python and FastAPI coding standards for the Fabric Unified Permission Hub'
---

# Python & FastAPI Standards

## Type Hints
- Always use `from __future__ import annotations` at the top of every module
- Use `str | None` syntax (not `Optional[str]`)
- Use Pydantic models for request/response validation at API boundaries

## FastAPI Patterns
- Route handlers are **synchronous** (`def`, not `async def`) — this app uses sync FastAPI
- Use `Request` parameter for template responses
- Use `HTTPException` for error responses with appropriate status codes
- Use dependency injection for shared services where appropriate

## HTTP Client
- Use `httpx.Client` (sync) with persistent connection pools — never `requests`
- Always set explicit `timeout` on all HTTP calls
- Always include `Authorization: Bearer {token}` headers for Azure APIs
- Never create a new `httpx.Client` per request — reuse module-level instances

## Error Handling
- Show user-friendly error messages, not raw stack traces
- Log errors with sufficient context for debugging
- Credential/token errors should suggest remediation (e.g., "run az login")
- Never expose tokens, secrets, or internal URLs in error responses

## Caching
- Use in-memory dict-based caches with TTL for expensive REST calls
- Cache keys must include entity identifiers (workspace ID, catalog name)
- Protect shared caches with `threading.Lock`
- Support `refresh=1` query parameter to bust caches

## Parallelism
- Use `concurrent.futures.ThreadPoolExecutor` for parallel I/O
- Set `max_workers` to match the number of concurrent tasks
- Never use `asyncio` — this is a synchronous application

## Security
- Never interpolate user input into URLs without validation
- Never log tokens or secrets
- Use `pydantic-settings` for configuration with `.env` files
- Validate route parameters with regex or Pydantic before use
