# Asset and backup policy

Assets are registered with a stable locator plus SHA-256 and byte size.  The
registry stores only metadata and supports `ephemeral`, `standard`, `critical`,
and `legal_hold` retention classes.  Signed URLs, query-string credentials,
fragments, and authorization material are rejected; use a stable object key or
path and resolve credentials at send time.

The read-only HTTP registry surface is workspace scoped. `GET /assets` (also
available as `/asset-registry`) lists registered asset metadata with checksum,
size, retention class, lifecycle status, and any available creative review
status. `GET /assets/detail?asset_id=...` returns one asset plus its recovery
audit history. These routes never fetch external locators or mutate registry,
review, or recovery state; callers must hold the normal `workspace_read`
capability and may only read a workspace visible to their identity.

Backups are made through the existing SQLite online backup API, never by copying
the live WAL.  Each independent backup receives a durable manifest containing
hash, size, schema version, and integrity result.  Verification re-hashes the
file and runs SQLite quick-check and foreign-key checks before marking it
verified.

Recovery plans reference only a verified manifest and an external provider
target.  RPO/RTO, scope, status, and status changes are audited.  Restore and
verification require the `backup_restore` capability; registration and manifest
creation require `backup_create`.  No recovery action is considered ready until
the manifest verification succeeds.
