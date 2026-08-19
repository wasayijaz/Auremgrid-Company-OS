# REST API reference

All responses are JSON except the dashboard. Errors use error and message fields with 400 validation, 403 authorization, 404 not found, or 500 internal status.

Read routes include /health, /search, /entity, /history, /neighbors, /sources, /recent, /brief, /work, /projects, /reviews, /decisions, /people, /signals, /risks, /opportunities, /meetings, /campaigns, /creative, /content, /finance, /notifications, /agents, /integrations, /memory-proposals, /knowledge-health, /dashboard/data, and /dashboard/client.

Write/action routes include /organizations, /workspaces, /people, /workspace-memberships, /projects, /deliverables, /reviews, /reviews/decide, /decisions, /signals, /signals/route, /risks, /opportunities, /health/calculate, /campaigns, /campaigns/metrics, /creative, /content, /content/advance, /approvals, /approvals/decide, /integrations, /reports/generate, /memory-proposals, /memory-proposals/review, and the /work action routes.

Organization-domain routes require organization_id and person_id. Evidence/legacy work routes require workspace_id and actor_id.

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
