# Contributing

This repository is a new system. Do not preserve backward compatibility. Remove obsolete paths instead of adding fallbacks.

## Local checks

Run tools/test.ps1 before committing. The suite must stay offline and must not require Docker, API keys, or private vault data.

## Boundaries

- Keep real client and employee data out of the repo.
- Keep Auremgrid as the authority for tenants, ACL, provenance, work state, and audit.
- Put Graphiti, Onyx, RAGFlow, LightRAG, Cognee, and Mem0 behind adapters.
- Prefer the smallest change that makes the operating loop stronger.


