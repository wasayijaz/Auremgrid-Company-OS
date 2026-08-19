# OSS Evaluation

All named engine implementations in the default repository are local style-compatible projections, not the upstream services. They are rebuildable and hold no unique canonical truth. Networked replacements remain optional and must preserve ACL-before-ranking, provenance, temporal accuracy, rebuildability, and offline degradation.

The default product uses local, deterministic stand-ins for Graphiti, Cognee, Mem0, Onyx, RAGFlow, LightRAG, GraphRAG, and Letta. None of those libraries is a required runtime dependency.

Evaluation order if a networked extra is added:

1. Keep the local baseline green.
2. Enable one extra behind a flag.
3. Compare temporal accuracy, provenance, ACL safety, latency, and operational cost.
4. Keep the extra only if it improves the baseline without becoming a second source of truth.
