---
last_updated: 2026-04-21T22:02:32.798Z
---

# Team Wisdom

Reusable patterns and heuristics learned through work. NOT transcripts — each entry is a distilled, actionable insight.

## Patterns

**Pattern:** Connection pooling eliminates REST latency. **Context:** Fabric and Databricks APIs where cold calls are 10-25s. Persistent httpx.Client pools reduced to sub-second cached.

**Pattern:** ThreadPoolExecutor parallelizes independent REST calls. **Context:** Tab data loading that fetches from multiple APIs (workspace + items + policies). 3 parallel calls beat 3 serial calls every time.

**Pattern:** Token caching with 5-min TTL per scope prevents redundant Azure AD roundtrips. **Context:** Every REST call needs a token; caching avoids 200-500ms per token acquisition.

**Pattern:** HTMX partial loading gives instant perceived load. **Context:** Full pages render a skeleton immediately, then HTMX fetches `_data` partial. Users see structure before data arrives.
