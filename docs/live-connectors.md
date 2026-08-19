# Live connector synchronization

Auremgrid has credential-backed read synchronization for Slack and ClickUp.
Google Drive and Gmail configuration contracts are available but deliberately
not enabled as live integrations yet. Their server-side contracts require a
read-only Google scope and explicit `folder:<id>` / `drive:<id>` or
`label:<id>` mappings. Integration responses report `live_enabled: false`, and
the enqueue boundary rejects a Google sync while that gate is closed. Enabled connectors use external secret references and durable
jobs; access tokens, refresh tokens, authorization headers, and provider
credentials are never stored in SQLite or job payloads.

Google provider verification and enqueue remain behind the same closed live
gate. Configuration and credential-reference binding do not ingest historical
files or messages; no Google content is represented as synchronized evidence.
The future Google execution path expects the referenced environment value to be
a JSON object containing exactly `client_id`, `client_secret`, and
`refresh_token`. It refreshes an access token in memory, requires provider-
reported granted scopes, and persists none of those four credential values.

## Connection lifecycle

1. Configure an integration with the exact expected provider account ID and
   explicit external-container to internal-workspace mappings. The integration remains `not_connected` regardless of
   any caller-supplied status.
2. Bind an `env:UPPER_CASE_NAME` credential reference. The response exposes a
   fingerprint and status, not the reference locator or secret value.
3. Verify the account identity and granted permissions against the provider.
   Successful verification changes
   the state to `authorized`, not `connected`.
4. Enqueue `connector.sync`. The worker resolves the secret at execution time,
   reauthorizes its principal, fetches the mapped stream, durably records the
   page in the connector inbox, ingests each new event, and only then promotes
   the cursor. The first successful pass changes the state to `connected`.

HTTP routes are `POST /integrations`, `/integrations/credentials`,
`/integrations/verify`, and `/integrations/sync`; `GET /integrations` returns
sanitized operational state. The old caller-controlled sync start/complete
routes were removed.

## Restart and retry behavior

Inbox dedupe keys are scoped by organization, provider account stream, event,
and revision. Replayed pages are harmless; edited objects create a new revision.
Failed or unfinished events block cursor promotion. Provider 429 responses and
transient transport failures become durable job retries, and provider delay
headers set the earliest retry time. Inbox events use leased, fenced retries;
an exhausted poison event is explicitly quarantined and leaves the integration
degraded rather than wedging the stream. Workers heartbeat during ingestion and
never sleep inside an adapter. One active job is allowed per immutable mapped
stream, and reconfiguration is blocked while such a job is active.

Slack maps channel IDs and uses a durable pagination/high-watermark cursor.
ClickUp maps list IDs, verifies each list belongs to the expected team, and
periodically restarts reconciliation from page zero. A worker processes up to 20
provider pages per run while heartbeating. If more remain, the integration stays
`authorized/backfilling` rather than claiming healthy completion.

The disabled Google adapters are held to a stricter release gate than a
successful API call: bounded historical backfill must cut over to Drive changes
or Gmail history without a gap; moves, removals, label exits, and deletions must
produce durable route lifecycle state; cursor expiry must rebootstrap safely;
and provider identity, permission, quota, authorization, and retry states must
remain explicit. Until those behaviors pass end-to-end integration tests, a
Google connection cannot enqueue work or report `connected`.

## Current deployment boundary

Credential binding is currently manual and environment-backed. Auremgrid does
not yet claim an in-product OAuth installation or
callback flow. That requires a write-capable external secret backend, OAuth
state and PKCE validation, refresh-token rotation, and provider-account
re-verification. CI uses injected transports and does not claim that a live
customer account was connected.
