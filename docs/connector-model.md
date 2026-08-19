# Connector model

Integration stores source, expected and verified provider account identity,
requested and granted permissions, explicit workspace mappings, last sync,
error, object count, and health. Per-mapping cursor records and ingest batches
track each immutable stream. The durable connector inbox owns event dedupe,
leases, retries, quarantine, and fenced cursor promotion.

Connector events first enter the evidence ingestion path. Slack and ClickUp are
enabled read integrations. Drive and Gmail are disabled change-feed adapter
contracts pending safe routing and backfill. Figma, GitHub, Fireflies, Meta Ads,
Google Ads, and finance providers remain catalog entries. Connectors never own
organization identity, permissions, canonical work state, approvals, or
financial truth.

The repository includes local Markdown and simulated source connectors for
offline demonstrations; they are not reported as live provider connections.
