# MCP-style tools

McpToolRouter is transport-neutral and requires a trusted
`AuthenticatedIdentity` from its transport. Organization, person, workspace,
and legacy actor arguments are checked against that identity before any service
lookup. It exposes:

- brain.search, brain.entity, brain.entity.candidates, brain.history, brain.neighbors, brain.sources, brain.recent
- brain.read, brain.health
- brain.propose (requires `brain_propose`), brain.promote (requires `brain_promote`), and brain.resolve_conflict (requires `brain_promote`)
- clients.list, clients.brief, clients.health, clients.roster.get, clients.roster.create
- projects.list, projects.get
- work.list, work.create, work.assign, work.update, work.review
- decisions.list, decisions.create
- meetings.list, meetings.get, meetings.responsibilities.get, meetings.responsibilities.set
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
- intelligence.profiles.list, intelligence.profiles.get
- intelligence.runbooks.list, intelligence.runbooks.get
- intelligence.orchestrator.run, intelligence.orchestrator.result
- intelligence.learning.get, intelligence.recommendations.quality, intelligence.hypotheses.record
- intelligence.recommendations.record, intelligence.recommendations.lifecycle
- intelligence.evaluation_safety.get, intelligence.evaluation.start, intelligence.evaluation.complete

The original short tool names remain internal aliases for the same handlers. Brain mutation tools use explicit proposal/promotion capabilities; they never fall back to `brain_read`. Proposer, reviewer, organization, person, workspace, and actor identity are derived or checked from the authenticated identity. Permissions are enforced by the service layer, not by tool naming.
`brain.read` returns the scoped canonical Brain view; `brain.health` returns its sanitized counts and provider-health subset. Both require `brain_read`, accept optional `as_of`, and never expose provider error details. `brain.entity.candidates` requires `brain_propose`; it returns only visible, citeable deterministic candidates and never creates a proposal or merge. Fact results from search, entity, and history include canonical `effective_state`; history and neighbors accept the same temporal fence.

The `intelligence.*` tools require `brain_read`. Profile and runbook tools
expose immutable ExpertProfile and Runbook contracts for the authenticated
workspace; optional filters mirror the REST API. `intelligence.orchestrator.run`
performs the bounded read-only orchestration over permitted Intelligence and
returns the trace, contributors, runbook route, contradictions, and degraded
state exactly as produced. `intelligence.orchestrator.result` retrieves a prior
result only through the scoped `trace_id` lookup. Learning tools persist
append-only hypotheses, recommendations, and lifecycle events behind
`brain_propose`/`brain_promote`; evaluation tools record shadow-only telemetry
and circuit state without changing agent routing. These tools do not execute
recommendations, enqueue proactive jobs, or mutate contract definitions.
`intelligence.recommendations.quality` is a `brain_read` aggregate over the
latest evaluated lifecycle event per recommendation. It reports correctness
rate and denominator only where score, measured outcomes, and evidence refs
are present, and exposes pending/insufficient counts without fabricating
outcomes.

The graph provider is local by default. An explicitly configured Graphiti/Neo4j
projection is queried only for full-workspace source access; partial ACLs skip
that channel and do not reduce canonical, FTS, or semantic retrieval. Health
reports configured/unavailable/degraded state and generation metadata only;
credentials, remote episode payloads, and raw provider errors never enter MCP.
Roster and meeting responsibility reads are workspace-scoped; roster creation and responsibility setting require `people_manage`. `people.capacity` requires `week_start` (an ISO Monday), accepts optional `workspace_id` and `as_of`, and returns a derived capacity board from canonical availability, leave, work, and workflow inputs. Caller identity is derived from the authenticated identity, and cross-workspace requests are rejected without disclosing records.

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
