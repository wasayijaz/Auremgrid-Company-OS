# MCP-style tools

McpToolRouter is transport-neutral and requires a trusted
`AuthenticatedIdentity` from its transport. Organization, person, workspace,
and legacy actor arguments are checked against that identity before any service
lookup. It exposes:

- brain.search, brain.entity, brain.history, brain.neighbors, brain.sources, brain.recent
- brain.propose (requires `brain_propose`) and brain.promote (requires `brain_promote`)
- clients.list, clients.brief, clients.health
- projects.list, projects.get
- work.list, work.create, work.assign, work.update, work.review
- decisions.list, decisions.create
- meetings.list, meetings.get
- campaigns.list, campaigns.get, campaigns.performance
- people.list, people.capacity
- risks.list, opportunities.list
- agents.list, agents.runs
- notifications.list
- reports.generate
- workflows.templates, workflows.runs.get, workflows.runs.create
- workflows.stages.start, workflows.stages.complete, workflows.evidence.add
- workflows.approvals.request, workflows.approvals.decide
- workflows.handoffs.acknowledge
- integrations.list, integrations.configure, integrations.credentials.bind
- integrations.verify, integrations.sync

The original short tool names remain internal aliases for the same handlers. Brain mutation tools use explicit proposal/promotion capabilities; they never fall back to `brain_read`. Proposer, reviewer, organization, person, workspace, and actor identity are derived or checked from the authenticated identity. Permissions are enforced by the service layer, not by tool naming.
Connector sync enqueues a durable job. Credential binding accepts only an
external `env:` reference; resolved credential material never enters MCP
arguments, results, jobs, or the ledger.

`integrations.list` returns sanitized provider identity, credential metadata,
health, and `live_enabled`. Google mapping contracts use `folder:<id>` or
`drive:<id>` for Drive and `label:<id>` for Gmail; configuration does not imply
provider verification or historical ingestion. `integrations.sync` is enabled
for Google only after account, scope, and mapping verification.

Google credential references are external `env:` bindings whose value is a
strict JSON bundle containing `client_id`, `client_secret`, and `refresh_token`.
The values and refreshed access token remain in memory and never enter MCP
arguments, results, jobs, or ledger records.
