# Feature Builder — History

## Project Context (Day 1)

- **Project:** Fabric Unified Permission Hub
- **Stack:** Python 3.11+, FastAPI, Uvicorn, Jinja2, HTMX, httpx, Pydantic v2
- **Owner:** maintainer
- **Structure:** `app/routers/` (7 routers), `app/services/`, `app/connectors/`, `app/templates/`
- **Key patterns:** Sync route handlers, HTMX partials with `_` prefix, ThreadPoolExecutor for parallel I/O, 5-min TTL caching
