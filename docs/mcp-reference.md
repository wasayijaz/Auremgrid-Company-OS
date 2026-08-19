# MCP-style tools

McpToolRouter is transport-neutral and requires a trusted
`AuthenticatedIdentity` from its transport. Organization, person, workspace,
and legacy actor arguments are checked against that identity before any service
lookup. It exposes:

- brain.search, brain.entity, brain.history, brain.neighbors, brain.sources, brain.recent
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

The original short tool names remain internal aliases for the same handlers. Permissions are enforced by the service layer, not by tool naming.
