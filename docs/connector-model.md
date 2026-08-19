# Connector model

Integration stores source, status, workspace mappings, permission scopes, cursor, last sync, error, object count, and health. SyncRun records each attempt and cursor transition.

Connector events first enter the Signal or evidence ingestion path. Slack, Drive, Gmail, ClickUp, Figma, GitHub, Fireflies, Meta Ads, Google Ads, and finance providers remain adapters. They never own organization identity, permissions, canonical work state, approvals, or financial truth.

The repository includes local Markdown and simulated source connectors. Provider authentication and production mappings are optional connector-specific modules.
