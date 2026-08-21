# Connector model

Integration stores source, expected and verified provider account identity,
requested and granted permissions, explicit workspace mappings, last sync,
error, object count, and health. Per-mapping cursor records and ingest batches
track each immutable stream. The durable connector inbox owns event dedupe,
leases, retries, quarantine, and fenced cursor promotion.

Connector events first enter the evidence ingestion path. Slack, ClickUp, Drive,
Gmail, and exact-file Figma polling are enabled read integrations with provider verification, durable
backfill/change checkpoints, lifecycle-aware routing, and redacted overlap
quarantine. Figma requires `current_user:read`, `file_metadata:read`, and
`file_content:read`; polling reads current metadata first and downloads content
only when the version changes. The fetched, version-fenced file snapshot can
retain bounded frame/section evidence. When `file_versions:read` is explicitly
configured and proven, a changed file can also retain one bounded page of
named-version evidence. The parent file is the only lifecycle and object-count
record. Figma does not ingest comments, model review or approval workflows, or auto-create
deliverables, reviews, or tasks. GitHub, Fireflies, Meta Ads, Google Ads, and
finance providers remain disabled catalog entries and have no live adapter or
integration wiring. Connectors never own organization identity, permissions,
canonical work state, approvals, or financial truth.

The repository includes local Markdown and simulated source connectors for
offline demonstrations; they are not reported as live provider connections.
