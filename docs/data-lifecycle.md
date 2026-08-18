# Data Lifecycle

1. Register a source artifact for one workspace.
2. Grant actors access to that source.
3. Ingest documents with content hashes and locators.
4. Extract deterministic fact observations.
5. Retrieve documents and facts through ACL-filtered queries.
6. Return evidence bundles with citations.

Re-ingesting the same `workspace_id`, `source_id`, and `content_hash` is skipped. New content creates new documents and new observations.
