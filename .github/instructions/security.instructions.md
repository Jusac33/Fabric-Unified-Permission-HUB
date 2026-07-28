---
applyTo: '**/*.py'
description: 'Security standards for Python/FastAPI applications handling Azure credentials and REST APIs'
---

# Security Standards

## OWASP Top 10 — Python/FastAPI Focus

| # | Category | Key Mitigation |
|---|----------|----------------|
| A01 | Broken Access Control | Validate route params, check workspace ownership |
| A02 | Security Misconfiguration | No debug in prod, no default SECRET_KEY |
| A03 | Injection | Never interpolate user input into URLs/queries |
| A04 | Cryptographic Failures | Use Azure Identity for tokens, never store plaintext |
| A05 | Security Logging Failures | Log auth failures, never log tokens/secrets |

## Credential & Token Security

- **Never log tokens**: Strip `Authorization` headers from any debug output
- **Thread-safe token cache**: Use `threading.Lock` around token cache access
- **Token scope validation**: Ensure the correct scope is used per API
  - Fabric: `https://api.fabric.microsoft.com/.default`
  - Databricks: `2ff814a6-3304-4ab8-85cb-cd0e6f879c1d/.default`
  - Graph: `https://graph.microsoft.com/.default`
- **Credential errors**: Show "Authentication failed — run 'az login'" not raw exceptions

## Input Validation

```python
# Validate workspace IDs (UUID format)
import re
UUID_RE = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')
if not UUID_RE.match(workspace_id):
    raise HTTPException(400, "Invalid workspace ID")

# Validate pairing IDs (hex string)
PAIRING_ID_RE = re.compile(r'^[a-f0-9]{10}$')
if not PAIRING_ID_RE.match(pairing_id):
    raise HTTPException(400, "Invalid pairing ID")
```

## REST Client Security

- All HTTP clients must use HTTPS endpoints only
- Set explicit timeouts: `httpx.Client(timeout=60.0)`
- Bound connection pools: `limits=httpx.Limits(max_connections=20)`
- Never pass raw user input as URL path segments without validation

## Configuration Security

- Secrets in `.env` only — never in source code
- `.env` must be in `.gitignore`
- Flag `SECRET_KEY = "change-me"` as insecure for production
- Validate required environment variables at startup via `pydantic-settings`

## Template Security

- Jinja2 autoescaping is enabled by default — never disable it
- Never render user-controlled template file paths
- Only pass sanitized data to template context
- HTMX partials should not expose internal IDs or system information

## Error Response Security

- Production errors: generic message + log details server-side
- Never expose: stack traces, file paths, token values, internal URLs
- Auth errors: suggest user action, not technical details
