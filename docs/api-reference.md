# REST API reference

All responses are JSON except the dashboard. Errors use error and message fields with 400 validation, 403 authorization, 404 not found, or 500 internal status.

Read routes include /health, /search, /entity, /history, /neighbors, /sources, /recent, /brief, /work, /projects, /reviews, /decisions, /people, /signals, /risks, /opportunities, /meetings, /campaigns, /creative, /content, /finance, /notifications, /agents, /integrations, /memory-proposals, /knowledge-health, /dashboard/data, and /dashboard/client.

Write/action routes include /organizations, /workspaces, /people, /workspace-memberships, /projects, /deliverables, /reviews, /reviews/decide, /decisions, /signals, /signals/route, /risks, /opportunities, /health/calculate, /campaigns, /campaigns/metrics, /creative, /content, /content/advance, /approvals, /approvals/decide, /integrations, /reports/generate, /memory-proposals, /memory-proposals/review, and the /work action routes.

Organization-domain routes require organization_id and person_id. Evidence/legacy work routes require workspace_id and actor_id.
