# Jobs, outbox, secrets, and recovery

Schema version 11 adds the production-control records needed before live
connectors are safe.

## Durable jobs

Jobs are scoped to an organization, optional workspace, and the principal that
requested them. Enqueue is idempotent by payload hash. A worker claims with an
atomic `BEGIN IMMEDIATE`, lease owner, expiry, and fencing token; stale workers
cannot heartbeat, complete, or fail a reclaimed job. Progress, bounded retry,
dead-letter state, cancellation, results, errors, and append-only events survive
process restarts.

The HTTP API can enqueue, list, inspect, and cancel allowlisted job types for a
caller with `job_manage`. Claiming is deliberately not public. `worker-once`
runs in a separate process/connection, re-authorizes the snapshotted principal,
executes one registered safe handler, and exits. This avoids an unbounded worker
thread inside the web process.

## Outbox

Outbox records provide at-least-once leased publishing with payload hashes,
idempotency keys, attempts, backoff, and fencing. They are the delivery primitive
for future live connector sends. Existing domain services still contain
scattered commits, so the repository does not yet claim every domain mutation
is transactionally paired with an outbox event; that refactor remains a hard
gate before externally visible sends.

## Secrets

SQLite stores only secret-binding metadata: external reference, provider,
scopes, fingerprint, status, and verification timestamps. The environment
secret store resolves `env:UPPER_CASE_NAME` only at authorized use time. API,
jobs, outbox, audit, and error payloads must carry binding IDs, never credential
values or authorization headers. Recursive redaction is defense in depth, not
permission to persist a secret.

Connector synchronization is a registered `connector.sync` worker job. Its
payload contains only the integration ID. Provider rate limits schedule a
durable retry using the provider delay when available; adapters do not sleep.
See [live connector synchronization](live-connectors.md).

## Backup and restore

Backups use SQLite's online backup API, then write a manifest containing SHA-256,
schema version, representative counts, size, and creation time. Verification
runs checksum, `quick_check`, and `foreign_key_check` against an independently
opened database.

Restore is an offline CLI action. It verifies the source, restores into a
temporary file, makes a safety backup before explicit overwrite, and atomically
replaces the destination. Restored sessions are revoked, in-flight jobs are
returned to retry state, `recovery_mode=1`, and outbound dispatch remains
disabled pending human reconciliation. External asset URLs are recorded by the
database but their binary objects require a separate asset backup policy.
