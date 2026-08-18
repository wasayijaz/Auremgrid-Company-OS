# OSS Evaluation

The default product uses local, deterministic stand-ins for Graphiti, Cognee, Mem0, Onyx, RAGFlow, LightRAG, GraphRAG, and Letta. None of those libraries is a required runtime dependency.

Evaluation order if a networked extra is added:

1. Keep the local baseline green.
2. Enable one extra behind a flag.
3. Compare temporal accuracy, provenance, ACL safety, latency, and operational cost.
4. Keep the extra only if it improves the baseline without becoming a second source of truth.
