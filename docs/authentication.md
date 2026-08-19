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

Initial session creation is local-only through `auremgrid bootstrap-auth`.
Session rotation, revocation, API-token creation, and `/auth/me` are authenticated
operations. The plaintext token is returned once and must be stored outside the
database.
