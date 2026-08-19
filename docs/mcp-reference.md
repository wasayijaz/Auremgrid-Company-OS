# MCP-style tools

McpToolRouter is transport-neutral. It exposes:

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

The original short tool names remain internal aliases for the same handlers. Permissions are enforced by the service layer, not by tool naming.
