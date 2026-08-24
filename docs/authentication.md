# Authentication and capabilities

`auremgrid.services.auth.AuthService` authenticates opaque sessions and API
tokens against the authentication tables introduced in schema v11: `auth_principals`, `auth_sessions`, and
`api_tokens` tables. Tokens are generated with `secrets`, persisted only as
SHA-256 digests, and compared with `hmac.compare_digest`. The plaintext token
is returned only at creation/rotation time.

Sessions and API tokens are distinct credential types. Both enforce expiry,
revocation, and the active status of the principal and person. API tokens also
carry an explicit scope set; effective capabilities are the intersection of
those scopes with role policy. Token authentication derives organization and
person identity from the principal record, never from caller-supplied IDs.

Role policy is deny-by-default. Capabilities distinguish workspace access,
finance, integrations, secrets, workflows, approvals, agents, automations,
brain promotion, external sends/publishing, jobs, backup/restore, and auth
management. Workspace admin/operator/viewer roles are restrictive boundaries,
so a viewer cannot write even when their organization role allows it. Use
`AuthService.authorize(identity, capability, organization_id, workspace_id)` at
the integration boundary to enforce both identity scope and capability.

Every JSON HTTP endpoint except health and the static dashboard shell requires
`Authorization: Bearer <opaque-token>`. Organization/person IDs are derived
from the credential; supplied mismatches are rejected. Legacy evidence APIs
derive `actor_id` through `principal_actor_bindings`. MCP transports must
construct `McpToolRouter` with an already-authenticated identity; tool arguments
cannot choose an identity.

Initial agency activation is local-only through `auremgrid setup-agency`. It
creates the organization, first workspace, owner membership, Brain actor
binding, principal, and first session as one operation. `bootstrap-auth` remains
the lower-level command for an existing person whose organization, workspace
membership, and actor already exist.
Session rotation, revocation, API-token creation, local-admin invite creation,
invite consumption, invite revocation, session inventory, session revoke-by-id,
and `/auth/me` are authenticated operations. Invite and session-management
routes require `auth_manage`; stored session, API, and invite tokens are hash
only. The plaintext token is returned once and must be stored outside the
database.

Local-admin invites are for controlled provisioning and recovery only. They
are expiry-bound, one-time records created by an authenticated administrator
for an existing person in the same organization. Consuming an invite still
requires an authenticated local administrator; it is not a public account
creation, signup, password reset, email, or magic-link flow.

## What the dashboard access token means

The access token is a temporary bearer credential: possession of it proves the
browser may act as one specific Auremgrid principal until the session expires
or is revoked. It is not an AI-provider key, a database password, or a shared
agency password. The dashboard stores it in that browser profile's
`localStorage` and sends it as `Authorization: Bearer <token>` on authenticated
requests.

Treat a token like a temporary password:

- issue one session per person and never share the owner's session;
- copy it only from the one-time setup or rotation response;
- never place it in URLs, screenshots, tickets, chat, logs, or source control;
- use **Sign out** to remove it from a browser, especially on a shared device;
- rotate or revoke it when a device is lost or access changes;
- use a scoped API token, stored in a secret manager, for integrations.

The local token flow is suitable for a controlled private deployment. Before
internet exposure, put Auremgrid behind HTTPS and a trusted reverse proxy and
define operator provisioning, device, revocation, backup, and incident-response
policies. A public multi-tenant product should use a dedicated identity provider
(for example OIDC/SSO or magic links) rather than expose a bootstrap endpoint.
Do not add unauthenticated token-minting routes such as public signup, invite
acceptance, password reset, or email-link recovery without that external
identity and delivery authority.
