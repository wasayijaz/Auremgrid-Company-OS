# Dashboard architecture

The dashboard is a local, light desktop-style operating application served by the Python HTTP process. Its desktop shell uses three zones: primary navigation, the operating canvas, and a permanent Auremgrid Intelligence rail. The rail becomes a drawer at narrower desktop widths. Work provides workspace navigation, board/list views, filters, lifecycle lanes, and a contextual detail inspector, while Command, Clients, Brain, Finance, Agents, and integration views remain first-class. It requires no frontend build or remote asset.

Global navigation covers Command, Clients, Work, Projects, Review, Campaigns, Content, Creative, Brain, Meetings, People, Finance, Agents, Automations, Reports, Integrations, and Settings.

The command payload is produced by DashboardService after organization and workspace authorization. It includes real client, work, review, risk, agent, automation, attention, and audit data. Finance shows Not connected until the finance connection ledger reports connected.

Ask Auremgrid uses `GET /dashboard/intelligence`, which composes permitted evidence and canonical operating records into inspectable situation, change, hypothesis, scenario, impact, and recommendation fields. Every finding carries confidence, citations, uncertainty/degraded state, and proposed action descriptors. Historical briefs are read-only; viewers receive no mutation actions. Query retrieval never claims causation from relevance alone. Work capture uses the same validated operating service as REST and MCP.

The Command overview also loads `GET /dashboard/intelligence/executive` for its portfolio brief. The permanent Intelligence rail exposes causal links, supporting and opposing hypotheses, modeled scenarios, historical analogues, and decision-to-outcome learning when those structures are supported. These remain derived read models; suggested operations travel through canonical, permission-checked routes and are never silently executed.
