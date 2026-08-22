# Secure integrations

Provider installations are tenant and workspace scoped and are keyed by
`provider + account_id`, so one organization may install several accounts for
Google, Slack, Figma, or GitHub.  Redirect URIs are exact-match allowlisted;
OAuth state expires, is single-use, and verifies S256 PKCE before a callback is
accepted.

Operational rows contain only environment references, encrypted vault blobs,
and SHA-256 metadata.  Access and refresh tokens, client secrets, webhook
secrets, authorization headers, and raw webhook payloads are not persisted.
The local vault requires `AUREMGRID_DEPLOYMENT_KEY` (or an explicit key passed
to `EncryptedSecretVault`) and fails closed when absent.

Webhook intake authenticates the provider-specific secret, records only event
and signature digests, deduplicates per installation, commits the durable event,
then invokes the enqueue callback.  Outbound sends require the `external_send`
capability and an approved `approval_requests` row.  The send intent and the
existing outbox event are inserted in one transaction, use a caller-supplied
idempotency key, and are blocked while recovery mode or disabled outbound
dispatch is active.  Transport dispatch is intentionally injectable so retry
and failure handling can be tested without a network call.
