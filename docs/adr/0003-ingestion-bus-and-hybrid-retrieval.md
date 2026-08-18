# ADR 0003: Ingestion Bus and Hybrid Retrieval Stay Behind Auremgrid

Status: accepted

## Context

The first slice proved tenant isolation, citations, temporal facts, and Cosmo work rules. The next gap is not more scaffolding. It is an ingestion bus and hybrid retrieval that can later host Slack, Drive, ClickUp, Figma, Graphiti, and vector search.

## Decision

Add a connector bus and a Graphiti-shaped local temporal graph now. Simulated Slack, Drive, ClickUp, and Figma connectors write into the same evidence contracts as local markdown. Hybrid retrieval fuses keyword, lexical-vector, and graph scores only after ACL filtering.

A networked Graphiti, RAGFlow, LightRAG, Onyx, Cognee, or Mem0 adapter may replace these engines later only if it beats this baseline on temporal accuracy, provenance, permission safety, and operational cost.

## Consequences

- Live credentials are not required to prove the architecture.
- Search results can show which channel contributed: keyword, vector, or graph.
- Future live connectors do not invent a second workflow or truth store.
