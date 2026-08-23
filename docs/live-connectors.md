# Live connector synchronization

Auremgrid has credential-backed read synchronization for Slack, ClickUp,
Google Drive, Gmail, explicitly mapped Figma files, and a single mapped
Fireflies account. Google server-side
contracts require a read-only Google scope and explicit `folder:<id>` /
`drive:<id>` or `label:<id>` mappings. Integration responses report
`live_enabled: true` only after the catalog gate; verification still requires
provider identity and granted-scope evidence. Enabled connectors use external
secret references and durable jobs; access tokens, refresh tokens,
authorization headers, and provider credentials are never stored in SQLite or
job payloads.

The Google execution path expects the referenced environment value to be
a JSON object containing exactly `client_id`, `client_secret`, and
`refresh_token`. It refreshes an access token in memory, requires provider-
reported granted scopes, and persists none of those four credential values.
The packaged Google OAuth support is deliberately generic and fail-closed:
operators must provide their own OAuth client registration, allowlisted
redirect, deployment key, and token-exchange transport. No Google client
credentials are bundled in the repository.

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

Google synchronization is held to a stricter gate than a successful API call:
bounded historical backfill captures a baseline before switching to Drive
changes or Gmail history; moves, removals, label exits, and deletions produce
durable route lifecycle state; cursor expiry reboots through a controlled
generation; and provider identity, permission, quota, authorization, and retry
states remain explicit. Drive move/reconciliation tasks are leased and fenced,
restartable, and only acknowledge a descendant wave after all spawned pages
drain. A file that matches mappings for different workspaces is quarantined at
organization scope using an opaque digest, with no object, content, count, or
workspace evidence written to either stream; the original cursor remains
parked until an operator resolves the mapping.

Figma synchronization is limited to explicit `file:<key>` mappings. Verification
proves the expected provider identity and the `current_user:read`,
`file_metadata:read`, and `file_content:read` grants. Each poll reads current
metadata first and downloads the file at that captured provider version only
when it differs from the durable cursor. That fetched, version-fenced snapshot
can retain bounded frame/section evidence. When `file_versions:read` is
explicitly configured and proven, a changed file can also retain one bounded
page of named-version evidence; the parent file is the sole lifecycle and
object-count record. Figma does not synchronize comments, model review or
approval workflows, or auto-create
deliverables, reviews, or tasks. Inaccessible previously seen files produce
tombstones; malformed or failed responses do not advance the cursor.

Fireflies synchronization requires exactly one `account:<id>` mapping to one
workspace, since a Fireflies API key is scoped to a single account with no
per-team or per-workspace filter and cannot fan transcripts out to multiple
mapped routes. Verification proves the expected provider account identity and
the `transcripts:read` scope. Each poll fetches transcripts from the durable
high-watermark date cursor, emits one bounded transcript event per meeting
with sanitized summary and sentence evidence, and advances the cursor to the
newest transcript date seen on that page. Fireflies has no delete/removal
signal, so it never emits tombstones; malformed responses do not advance the
cursor.

## Current deployment boundary

Credential binding is currently manual and environment-backed. Auremgrid does
not yet claim bundled provider app registrations or managed installation for a
customer account. Generic PKCE state, callback, local-vault storage, health,
and revoke routes exist, but the default token-exchange transport raises rather
than pretending to connect. Public webhook intake and refresh-token rotation
are not part of the packaged demo. CI uses injected transports and does not
claim that a live customer account was connected.

Read-only Stripe Billing/accounting and Meta Ads adapters normalize injected
provider pages into immutable provider import records with cursor/quarantine
state. They are not live registered connector entries, do not appear as
`live_enabled` catalog connectors, and never send or mutate provider data.
