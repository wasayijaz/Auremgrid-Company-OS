# ADR 0004: Local Projections Are Not Second Brains

Status: accepted

## Context

Graphiti, Cognee, Mem0, Onyx, RAGFlow, LightRAG, GraphRAG, and Letta all do useful work, but wiring them as required runtimes would force extra databases, models, and unofficial truth stores onto every agency.

## Decision

Use all eight in the default path as in-process projections. Auremgrid remains the only system that can say what is true, who may see it, when it was valid, and whether work may move. Citations always resolve to an Auremgrid source. A networked extra is optional and must beat the local baseline.

## Consequences

- Any agency can onboard with Python and SQLite only.
- Search still works if an extra is missing.
- Preference memory, agent identity, and client facts stay in different stores.
