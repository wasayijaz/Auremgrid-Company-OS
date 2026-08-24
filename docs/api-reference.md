# REST API reference

All responses are JSON except the dashboard. Errors use error and message fields with 400 validation, 403 authorization, 404 not found, or 500 internal status.

`POST /webhooks/provider/<installation_id>` is a public provider receipt
boundary, disabled unless `AUREMGRID_WEBHOOK_RECEIPTS_ENABLED=1`. It accepts
the provider signature, timestamp, and event-id headers, enforces the
installation's configured secret and replay window, and returns only an event
digest/status. It does not require a bearer token, does not echo payloads, and
does not write to the provider; configure ingress/TLS and an installation
secret before enabling it.

Read routes include /health, /search, /entity, /entity/candidates, /history, /neighbors, /sources, /recent, /brief, /work, /projects, /reviews, /reviews/annotations, /decisions, /people, /capacity, /clients/roster, /meetings/responsibilities, /signals, /risks, /opportunities, /meetings, /campaigns, /creative, /content, /finance, /notifications, /agents, /integrations, /onboarding/templates, /onboarding/imports, /memory-proposals, /knowledge-health, /dashboard/data, /dashboard/client, /dashboard/brain, /dashboard/intelligence, /dashboard/intelligence/portfolio, /dashboard/intelligence/executive, /dashboard/intelligence/profiles, /dashboard/intelligence/profiles/get, /dashboard/intelligence/runbooks, /dashboard/intelligence/runbooks/get, /dashboard/intelligence/orchestrator/result, /dashboard/intelligence/learning, /dashboard/intelligence/recommendation-quality, /dashboard/intelligence/recommendations/quality, /dashboard/intelligence/evaluation-safety, /dashboard/intelligence/snapshots, /dashboard/intelligence/attention, and /dashboard/workflows.

`GET /dashboard/intelligence` requires `organization_id`, `workspace_id`, and `person_id`, accepts optional `query` and timezone-aware `as_of`, and enforces the bearer identity's organization, workspace, person, capability, and source ACL. It returns a derived read model rather than new canonical truth. The response exposes status, degraded reason, evidence-backed findings, confidence, changes, hypotheses, scenarios, impact, recommendation, and proposed action descriptors. If `CompanyOS` is constructed with an injected strategic-reasoning provider, `deliberation` may additionally contain validated `hypotheses`, `options`, `scenarios`, `recommendation`, `confidence`, and `dissent`; `provider_metadata` contains only provider identity, evidence references/counts, hashes, and fallback status. Provider failures or malformed output retain deterministic deliberation. Unknown queries return `insufficient_evidence`; historical reads and read-only memberships return no mutation actions.

`GET /dashboard/intelligence/portfolio` and `GET /dashboard/intelligence/executive` require `organization_id` and `person_id`. They aggregate only workspaces visible to that membership. The executive response adds attention, client-health, and downside-constraint sections. Finance remains explicitly `not_connected` with null values unless canonical connected finance evidence exists.

`GET /dashboard/intelligence/profiles` and
`/dashboard/intelligence/runbooks` require `organization_id`,
`workspace_id`, and `person_id`; they return immutable ExpertProfile and
Runbook definitions filtered to the bearer identity's workspace access and
capabilities. Profile lists accept optional `domain` and `capability_level`;
runbook lists accept optional `domain` and `profile_id`. The `/get` variants
also require `profile_id` or `runbook_id` and accept optional `version`.

`POST /dashboard/intelligence/orchestrator/run` requires
`organization_id`, `workspace_id`, and `person_id`, with optional `query`,
`runbook_id`, `profile_ids`, timezone-aware `as_of`, and bounded
`iterations`. It performs a read-only expert orchestration over the scoped
Intelligence projection and returns a result with `trace_id`, `status`,
`runbook_route`, contributing profiles, contradictions, trace, limits, and
scope. `GET /dashboard/intelligence/orchestrator/result` requires the same
scope plus `trace_id` and uses the scoped orchestrator lookup; mismatched
workspaces or people are denied without leaking the run. These routes do not
enqueue proactive jobs, execute recommendations, or write canonical truth.

`GET /dashboard/intelligence/learning` returns workspace-scoped
interpretation hypotheses, recommendations, and recommendation lifecycle
events. Hypotheses are displayed separately from facts and are never promoted
to canonical truth by these routes. `POST /dashboard/intelligence/hypotheses`
and `/dashboard/intelligence/recommendations` require `brain_propose` and
write only append-only learning records through the existing service gates.
`POST /dashboard/intelligence/recommendations/lifecycle` requires
`brain_promote` and appends accepted, rejected, chosen, or evaluated events;
it does not execute the recommendation.

`POST /dashboard/intelligence/recommendations/handoff` requires
`brain_propose` and records a human-reviewed, trace-linked recommendation
handoff. Supplied decision, approval, work, outcome, and action-descriptor
references are validated in the same workspace; the route creates none of
those canonical records or actions.

`GET /dashboard/intelligence/recommendation-quality` (also available as
`/dashboard/intelligence/recommendations/quality`) returns a read-only,
workspace-scoped correctness aggregate. Its denominator includes only the
latest evaluated event per recommendation with a score, measured outcomes,
and evidence references; scores of at least `0.5` count as correct. The
response includes correctness rate, denominator, evaluation window, pending
count, and insufficient-evidence count. Unevaluated or uncited rows are never
inferred to be correct or incorrect.

`GET /dashboard/intelligence/evaluation-safety` returns the shadow-only
evaluation decision, policy/circuit state, recent scoped evaluation runs, and
circuit events. `POST /dashboard/intelligence/evaluation/start` requires
`brain_propose`; `/dashboard/intelligence/evaluation/complete` requires
`brain_promote` and verifies the evaluation belongs to the supplied workspace.
Evaluation safety records telemetry and breaker state only; it does not alter
agent routing.

`POST /dashboard/intelligence/refresh` enqueues a read-only durable refresh for
an `executive` or workspace snapshot. `GET /dashboard/intelligence/snapshots`
returns the latest immutable per-person snapshot; `GET
/dashboard/intelligence/attention` returns its persisted top-three attention
queue. Workspace reads require the same workspace membership as live
Intelligence. Manual refreshes create a fresh job; callers may provide an
explicit idempotency key when they need request deduplication. A refresh whose
full reader-facing projection has not changed does not append another snapshot.
Processing requires `worker-once`; none of these routes executes an external
action.

Brain reads preserve the authenticated source ACL and optional `as_of` fence. Fact objects returned by `/search`, `/entity`, and `/history` include their canonical `effective_state`; `/neighbors` also returns the effective read timestamp. `/entity/candidates` requires `brain_propose` and returns only deterministic, evidence-backed name/domain candidates whose sources are visible to the caller; it creates no alias, proposal, or merge. `/knowledge-health` is a read-only summary of fact counts plus sanitized semantic and graph provider health. It reports deterministic semantic fallback explicitly and reports a degraded graph even when the last verified generation remains available.

Graph health is truthful about the optional upstream projection: an
unconfigured or unavailable Graphiti/Neo4j provider is not presented as a
remote connection. Upstream graph reads are omitted for partial source ACLs;
the response still includes canonical, FTS, and semantic results. Graph
generation, building, and stale-serving indicators are sanitized and contain
no credentials, provider error details, or episode content.

`GET /capacity` returns a derived weekly capacity board. It requires `week_start` (an ISO Monday), and accepts optional `workspace_id` and `as_of`. Organization and person are always derived from the bearer session; inaccessible workspaces are omitted.

Write/action routes include /organizations, /workspaces, /people, /workspace-memberships, /clients/roster, /meetings/responsibilities, /projects, /deliverables, /reviews, /reviews/decide, /reviews/annotations, /reviews/annotations/resolve, /reviews/annotations/supersede, /decisions, /signals, /signals/route, /risks, /opportunities, /health/calculate, /campaigns, /campaigns/metrics, /creative, /content, /content/advance, /approvals, /approvals/decide, /integrations, /integrations/credentials, /integrations/verify, /integrations/sync, /provider-imports/preview, /provider-imports/sync, /reports/generate, /reports/portal-publish, /reports/portal-revoke, /memory-proposals, /brain/propose, and the /work action routes. Rich review annotations are append-only and support general comments plus source-gated image points/regions, document pages/regions, and video timestamps/ranges. `POST /reviews/annotations` accepts an optional `idempotency_key`; resolve/supersede add immutable annotation events rather than overwriting history. `POST /clients/roster` accepts `roles` and optional `effective_at`/`note`; `POST /meetings/responsibilities` accepts a meeting ID and optional facilitator/note-taker IDs. These writes require `people_manage`. `POST /brain/propose` derives scope and proposer from the bearer identity and creates a pending alias/merge resolution proposal only; it never merges. `POST /memory-proposals` derives the proposer and scope from the bearer identity; the former `/memory-proposals/review` route is retired with 404. Use the authenticated MCP `brain.promote` service path for proposal decisions.

CSV onboarding routes are preview-first and append-only. `GET
/onboarding/templates` returns the supported `client_workspaces`, `campaigns`,
and `campaign_metrics` headers. `POST /onboarding/imports/preview` accepts
`import_type`, optional `workspace_id` for workspace-scoped imports,
`csv_text`, and `idempotency_key`; it parses with the standard CSV format,
records a durable dry-run batch, quarantines invalid rows, and writes no
canonical business records. `POST /onboarding/imports/commit` accepts
`batch_id` and a separate `idempotency_key`; it commits only valid preview rows
through existing public business methods and records an immutable receipt.
Uploads never accept arbitrary local filesystem paths.

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
mapping keys. Google enqueue is available when `live_enabled` is true and the
integration has been provider-verified; historical ingestion remains blocked
until that verification succeeds.

A bound Google environment reference must resolve to a strict JSON credential
bundle with exactly `client_id`, `client_secret`, and `refresh_token`. The
execution path refreshes access in memory and fails when Google does
not return authoritative granted-scope evidence; credential components and
ephemeral access tokens are not returned or persisted.

Generic OAuth routes exist for a fail-closed lifecycle. `/oauth/begin` creates
a single-use PKCE state only for an allowlisted redirect and caller-provided
client ID. `/oauth/callback` does not ship a Google token exchange; it succeeds
only when `CompanyOS` has an injected exchange transport and the local vault can
seal the resulting token with `AUREMGRID_DEPLOYMENT_KEY` or an explicit
deployment key. `/oauth/install/{id}/health` reports stored-token health without
returning token material, and `/oauth/revoke` revokes the local vault entry.
The repository does not bundle provider client credentials.

`POST /provider-imports/preview` and `/provider-imports/sync` are read-only
normalization paths for injected Stripe Billing/accounting and Meta Ads pages.
They require `integration_sync`, an account-to-workspace mapping, and a
supported resource. Preview uses fixture/injected pages only; sync writes
append-only `provider_import_records`, cursor state, and quarantine records.
These routes are not live registered connectors and never send or mutate
provider data.

Portal report routes are publication gates, not sends. `POST
/reports/portal-publish` requires a completed report run and an approved human
`report.portal_publish` approval request whose payload matches the report run.
It creates an immutable portal report version and supersedes the previous
version of that report type for the workspace. `POST /reports/portal-revoke`
records a portal revoke event. Client identities use `GET
/client-portal/reports`, `/client-portal/reports/view`, and
`/client-portal/reports/download`; those reads record portal view/download
events and return the approved snapshot only. No route emails, uploads, or
dispatches the report externally.

Organization-domain routes require organization_id and person_id. Evidence/legacy work routes require workspace_id and actor_id.

All JSON routes except `/health` require a bearer session or API token. Caller
identity is derived from the credential; organization, person, and legacy actor
arguments cannot override it. `/auth/me`, `/auth/api-tokens`,
`/auth/sessions/rotate`, `/auth/revoke`, and `/auth/actor-bindings` expose the
authenticated lifecycle without returning stored token hashes. `/auth/invites`,
`/auth/invites/revoke`, `/auth/invites/consume`, `/auth/sessions`, and
`/auth/sessions/revoke` are local-admin `auth_manage` operations for existing
people only. Invite creation uses `target_person_id` for the person being
recovered/provisioned; `person_id` remains caller-derived and cannot be forged.
They do not send email and must not be exposed as public signup, password
reset, public invite acceptance, or unauthenticated token-minting routes.

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
