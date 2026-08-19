# Dashboard architecture

The dashboard is a local, dark, dense command-center application served by the Python HTTP process. It requires no frontend build or remote asset.

Global navigation covers Command, Clients, Work, Projects, Review, Campaigns, Content, Creative, Brain, Meetings, People, Finance, Agents, Automations, Reports, Integrations, and Settings.

The command payload is produced by DashboardService after organization and workspace authorization. It includes real client, work, review, risk, agent, automation, attention, and audit data. Finance shows Not connected until the finance connection ledger reports connected.

Ask Auremgrid uses the same ACL-first evidence API. Work capture uses the same validated operating service as REST and MCP.
