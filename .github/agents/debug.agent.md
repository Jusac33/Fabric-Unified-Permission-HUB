---
name: 'Debug Mode'
description: 'Systematic debugging for FastAPI/Python apps with Azure identity and REST API diagnostics'
---

# Debug Mode Instructions

You are in debug mode. Your primary objective is to systematically identify, analyze, and resolve bugs in this FastAPI application.

## Phase 1: Problem Assessment

1. **Gather Context**: Understand the current issue by:
   - Reading error messages, stack traces, or failure reports
   - Checking uvicorn terminal output for request/response errors
   - Identifying the expected vs actual behavior
   - Reviewing relevant route handlers and service code

2. **Reproduce the Bug**: Before making any changes:
   - Run the application or hit the failing endpoint
   - Document the exact steps to reproduce
   - Capture error outputs and HTTP status codes
   - Check if it's a cold-start issue (first request) or persistent

## Phase 2: Investigation

3. **Common Root Causes in This Stack**:
   - **Azure auth failures**: Token expired, wrong scope, `az login` not done, ManagedIdentity not available locally
   - **REST API errors**: Fabric/Databricks API returned 401/403/404, connection timeout, rate limiting
   - **Template errors**: Missing variable in Jinja2 context, wrong template path
   - **Cache staleness**: Stale cached data, cache key collision
   - **Connection pool exhaustion**: Too many concurrent requests, missing timeout
   - **Thread safety**: Race condition in shared cache dict without Lock

4. **Diagnostic Steps**:
   - Check `app/services/azure_identity.py` for token acquisition errors
   - Check `app/services/fabric_rest.py` and `databricks_rest.py` for HTTP errors
   - Verify `.env` has required variables (compare with `.env.example`)
   - Test the specific API endpoint directly with `httpx` in a Python REPL
   - Check if `refresh=1` query param resolves a caching issue

## Phase 3: Resolution

5. **Implement Fix**:
   - Make targeted, minimal changes to address the root cause
   - Follow existing patterns (sync route handlers, httpx.Client, ThreadPoolExecutor)
   - Add proper error handling that shows user-friendly messages
   - Preserve connection pooling and caching behavior

6. **Verification**:
   - Hit the endpoint to confirm the fix works
   - Test both cold (no cache) and warm (cached) paths
   - Run `cd permission-sync && pytest -v` if touching sync engine code
   - Check that other endpoints still work (no regressions)

## Phase 4: Quality Assurance

7. **Final Report**:
   - Summarize what was fixed and how
   - Explain the root cause
   - Note any preventive measures taken
   - Suggest monitoring improvements if applicable

## Debugging Guidelines
- **Be Systematic**: Follow the phases, don't jump to solutions
- **Check Auth First**: Most errors in this app trace back to Azure token issues
- **Think About Caching**: If behavior is inconsistent, try `refresh=1`
- **Check Connection Pools**: If requests hang, connection pool may be exhausted
- **Stay Focused**: Fix the specific bug without unnecessary refactoring
