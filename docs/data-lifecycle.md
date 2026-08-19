# Data Lifecycle

External events first become SourceArtifact/Document evidence or a Signal. Classification routes a signal to a proposal, work item, risk, decision, notification, or approval. Uncertain extraction cannot enter canonical facts directly.

Canonical rows are durable and audited. Projection state records rebuild health and counts. Entity merges retain merge history. Proposal review retains the proposer, evidence, reviewer, decision time, and promoted record ID.

1. Register a source artifact for one workspace.
2. Grant actors access to that source.
3. Ingest documents with content hashes and locators.
4. Extract deterministic fact observations.
5. Retrieve documents and facts through ACL-filtered queries.
6. Return evidence bundles with citations.

Re-ingesting the same `workspace_id`, `source_id`, and `content_hash` is skipped. New content creates new documents and new observations.
