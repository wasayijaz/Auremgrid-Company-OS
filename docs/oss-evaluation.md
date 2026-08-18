# OSS Evaluation

The first milestone intentionally ships without Graphiti, Onyx, RAGFlow, LightRAG, Cognee, or Mem0 dependencies.

Evaluation order:

1. Establish the deterministic baseline.
2. Add one adapter behind a feature flag.
3. Compare temporal accuracy, provenance preservation, permission behavior, latency, and operational complexity.
4. Keep the adapter only if it improves the baseline without weakening Auremgrid-owned contracts.
