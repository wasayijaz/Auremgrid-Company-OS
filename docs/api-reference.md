# REST API reference

All responses are JSON except the dashboard. Errors use error and message fields with 400 validation, 403 authorization, 404 not found, or 500 internal status.

Read routes include /health, /search, /entity, /history, /neighbors, /sources, /recent, /brief, /work, /projects, /reviews, /decisions, /people, /signals, /risks, /opportunities, /meetings, /campaigns, /creative, /content, /finance, /notifications, /agents, /integrations, /memory-proposals, /knowledge-health, /dashboard/data, and /dashboard/client.

Write/action routes include /organizations, /workspaces, /people, /workspace-memberships, /projects, /deliverables, /reviews, /reviews/decide, /decisions, /signals, /signals/route, /risks, /opportunities, /health/calculate, /campaigns, /campaigns/metrics, /creative, /content, /content/advance, /approvals, /approvals/decide, /integrations, /integrations/credentials, /integrations/verify, /integrations/sync, /reports/generate, /memory-proposals, /memory-proposals/review, and the /work action routes.

Connector configuration requires the expected provider account ID and never accepts a connection state. Credential responses
contain metadata and fingerprints only. Verification earns `authorized`; the
first successful durable worker sync earns `connected`. Sync requests return a
workspace-scoped `connector.sync` jobs rather than performing provider I/O in the HTTP process.

Integration responses include `live_enabled`; callers must use this field
rather than infer availability from catalog presence or an `authorized`
credential. Google Drive configuration requires a read-only Drive scope and
mapping keys `folder:<id>` or `drive:<id>`; its expected account ID is the
stable Drive `permissionId`, not an email address. Gmail requires
`gmail.readonly`, a normalized expected mailbox address, and `label:<id>`
mapping keys. Google enqueue is rejected while
`live_enabled` is false; provider verification and historical ingestion are not
claimed while that gate is closed.

A bound Google environment reference must resolve to a strict JSON credential
bundle with exactly `client_id`, `client_secret`, and `refresh_token`. The
closed-gate execution path refreshes access in memory and fails when Google does
not return authoritative granted-scope evidence; credential components and
ephemeral access tokens are not returned or persisted.

Organization-domain routes require organization_id and person_id. Evidence/legacy work routes require workspace_id and actor_id.

All JSON routes except `/health` require a bearer session or API token. Caller
identity is derived from the credential; organization, person, and legacy actor
arguments cannot override it. `/auth/me`, `/auth/api-tokens`,
`/auth/sessions/rotate`, `/auth/revoke`, and `/auth/actor-bindings` expose the
authenticated lifecycle without returning stored token hashes.

`GET /jobs` and `/jobs/get` inspect scoped durable work. `POST /jobs` accepts an
allowlisted job type and idempotency key; `/jobs/cancel` cancels only queued or
retry-wait work. Claim/lease operations are worker-only and are not public HTTP
endpoints.

## Cross-wing workflows

Read routes:

- `GET /workflows/templates` lists the validated neutral catalog and accepts an optional `wing` filter.
- `GET /workflows/runs` lists runs in one permitted workspace.
- `GET /workflows/runs/get` returns the immutable snapshot, stages, progress, and transition history.
- `GET /workflows/escalations` returns overdue active runs and stages.

Write routes:

- `POST /workflows/runs` creates an idempotent run from `template_id`.
- `POST /workflows/stages/start` and `/workflows/stages/complete` enforce dependencies, handoffs, evidence, and approvals.
- `POST /workflows/evidence` records locator-backed or canonical-object evidence.
- `POST /workflows/approvals/request` links an evidence-complete stage to a pending canonical `approval_request`; `/workflows/approvals/decide` records the already-authorized canonical decision in workflow history and routes rejection to rework.
- `POST /workflows/handoffs/acknowledge` records acceptance of the artifact contract between wings.
- `POST /workflows/stages/block` and `/workflows/runs/cancel` preserve the reason and audit history.

Workflow routes require `organization_id`, `workspace_id`, and `person_id` except catalog discovery, which requires organization membership. Action routes also require `run_id` plus the relevant stage identifier. External triggers should send an `idempotency_key`.
