# Phase 14 Activation Requirements

The repository now contains the safe local control plane for provider
installations, PKCE verifier storage, webhook receipt fencing, outbound-send
approval fencing, asset metadata/recovery records, local administrator
recovery, and provider-import preview/sync boundaries. It does not include
credentials, public hosting, or live-provider authority.

## Requires user-owned credentials or account authority

Before activating any provider installation, an organization operator must
provide and approve:

- A registered provider application, client ID, redirect URL, allowed scopes,
  account identity, and test account for each OAuth provider.
- A write-capable secret backend and its deployment key or workload identity;
  credentials must never be stored in repository configuration or returned by
  API responses.
- Provider-specific token-exchange, pagination, rate-limit, retry, deletion,
  reconciliation, and conflict-resolution contracts.
- A sender transport and approved sender identity before any external outbound
  delivery is enabled.
- Explicit authorization for canonical writes produced by live CRM, advertising,
  or accounting imports. Preview is deliberately non-persisting; sync is the
  only persistent local path.

No live CRM adapter is bundled. Meta is an injected read-only adapter and
Google Ads is catalog-only. Stripe imports are a read-only external input,
not a substitute for QuickBooks, Xero, NetSuite, or reconciled accounting.

## Requires user-owned infrastructure

Before public or multi-user hosted operation, an operator must supply:

- A public hostname, TLS termination, reverse proxy, firewall/WAF, rate limits,
  monitoring, incident ownership, and a recovery/support policy.
- A hosted identity provider and verified email/SMS transport if public
  self-service account recovery is wanted. The local invite flow intentionally
  does not provide either capability.
- Managed PostgreSQL only after a dedicated portability project replaces the
  current SQLite-specific migration and query assumptions (FTS5, PRAGMAs,
  SQLite syntax, and transaction behavior).
- Object storage, CDN/distribution, signed delivery URLs, upload/download
  handling, malware/MIME scanning, media processing, retention/legal-hold
  workers, and a tested restore executor for external binary assets.

## Requires explicit human approval at runtime

- Any external send or connector write.
- Any one-way provider action, deletion, or public webhook activation.
- Any approval-gated reversible agent action; a matching scoped approval and
  idempotency key remain mandatory.
- Any recovery operation that changes operational state after a backup or
  incident.

## Evidence before activation

Record the approved provider/application identity, scope set, account-to-
workspace mapping, secret-reference health, TLS/public ingress test, webhook
signature test, least-privilege test account, rollback procedure, and a
successful recovery rehearsal. Do not mark a connector connected or a hosted
deployment production-ready until those records exist.
