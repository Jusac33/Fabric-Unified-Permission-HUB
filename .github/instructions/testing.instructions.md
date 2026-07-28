---
applyTo: 'permission-sync/**/*.py'
description: 'Testing standards for the permission-sync engine using pytest'
---

# Testing Standards (pytest)

## Running Tests

```powershell
cd permission-sync
pytest -v
```

## Coverage

```powershell
cd permission-sync
pytest --cov --cov-report=term-missing
```

## Test Structure
- Tests live in `permission-sync/tests/`
- Fixtures in `conftest.py`
- Test files follow `test_*.py` naming
- Test functions follow `test_*` naming

## Patterns
- Use `pytest.fixture` for shared setup (mock data, clients, configs)
- Mock external API calls — never hit real Fabric/Databricks APIs in tests
- Use `unittest.mock.patch` to mock REST client methods
- Test both success and error paths
- Test edge cases: empty results, missing permissions, auth failures

## What to Test
- Diff engine: `test_diff_engine.py` — permission comparison logic
- Discovery: `test_discovery.py` — Fabric workspace/item discovery
- Identity: `test_identity.py`, `test_identity_resolver.py` — email/OID resolution
- Sync engine: `test_sync_engine.py` — permission sync operations
- Privilege normalizer: `test_privilege_normalizer.py` — role mapping

## Assertions
- Be specific: assert exact values, not just truthiness
- Check both the result and side effects (e.g., API calls made)
- Use `pytest.raises` for expected exceptions
