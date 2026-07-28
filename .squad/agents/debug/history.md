# Debug Mode — History

## Project Context (Day 1)

- **Project:** Fabric Unified Permission Hub
- **Stack:** Python 3.11+, FastAPI, Azure Identity (ChainedTokenCredential)
- **Owner:** maintainer
- **Auth:** Per-scope token caching with 5-min TTL, credential warm-up on startup
- **Known patterns:** Device-code az login for local dev, MSI for deployed
