# Secure integrations

Provider installation rows, where used, are tenant and workspace scoped. The
packaged HTTP surface currently exposes a generic OAuth lifecycle for Google:
redirect URIs are exact-match allowlisted, OAuth state expires, is single-use,
and verifies S256 PKCE before a callback is accepted. The repository does not
bundle provider client credentials or a production token-exchange transport;
without an injected transport and deployment key, OAuth completion fails
closed.

Operational rows contain only environment references, encrypted vault blobs,
and SHA-256 metadata.  Access and refresh tokens, client secrets, webhook
secrets, authorization headers, and raw webhook payloads are not persisted.
The local vault requires `AUREMGRID_DEPLOYMENT_KEY` (or an explicit key passed
to `EncryptedSecretVault`) and fails closed when absent.

Webhook intake and outbound sends are service primitives with injected
transports, not packaged live provider integrations. Webhook intake
authenticates the provider-specific secret, records only event and signature
digests, deduplicates per installation, commits the durable event, then invokes
the enqueue callback. Outbound sends require the `external_send` capability and
an approved `approval_requests` row. The send intent and the existing outbox
event are inserted in one transaction, use a caller-supplied idempotency key,
and are blocked while recovery mode or disabled outbound dispatch is active.
Transport dispatch is intentionally injectable so retry and failure handling
can be tested without a network call.

The packaged HTTP receipt boundary is `POST /webhooks/provider/<installation>`
and is disabled unless `AUREMGRID_WEBHOOK_RECEIPTS_ENABLED=1` is explicitly
configured. It accepts a bounded body, validates the installation secret,
timestamp window, and event dedupe through `WebhookIntakeService`, and returns
only status plus a digest. Rejected receipts are quarantined as rejected event
metadata; accepted receipts are durable intake records. No raw body, secret,
or provider write is emitted, and the internal metrics use fixed low-cardinality
names (`webhook.receipt.*`).
