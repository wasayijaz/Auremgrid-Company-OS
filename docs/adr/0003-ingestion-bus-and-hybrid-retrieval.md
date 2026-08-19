# ADR 0003: Ingestion Bus and Hybrid Retrieval Stay Behind Auremgrid

Status: accepted

## Context

The first slice proved tenant isolation, citations, temporal facts, and agency work rules. The next gap is not more scaffolding. It is an ingestion bus and hybrid retrieval that can later host Slack, Drive, ClickUp, Figma, Graphiti, and vector search.

## Decision

Add a connector bus and a Graphiti-shaped local temporal graph now. Simulated Slack, Drive, ClickUp, and Figma connectors write into the same evidence contracts as local markdown. Hybrid retrieval independently runs SQLite FTS and a workspace-scoped semantic vector index only after ACL and temporal document filtering, then fuses keyword, semantic, and graph scores after canonical rehydration.

Schema 16 stores rebuildable float32 document vectors keyed by workspace, document, provider, model, version, and dimensions. The provider boundary defaults to an offline deterministic lexical fallback and can be injected with a local-files-only open-source model. Provider failures are visible as a degraded semantic channel; the system never silently substitutes a fallback or lets semantic ranking bypass authorization.

Schema 17 adds fenced graph projection generations. Graph providers receive workspace, allowed source IDs, and temporal bounds; external references are rehydrated against canonical evidence before ranking. Graph rebuild failure never rolls back canonical ingestion, and an incomplete generation cannot replace the active one.

A networked Graphiti, RAGFlow, LightRAG, Onyx, Cognee, or Mem0 adapter may replace these engines later only if it beats this baseline on temporal accuracy, provenance, permission safety, and operational cost.

## Consequences

- Live credentials are not required to prove the architecture.
- Search results can show which channel contributed: keyword, vector, or graph, including degraded semantic health.
- Future live connectors do not invent a second workflow or truth store.

