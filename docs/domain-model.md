# Domain model

Organization is the tenant boundary. It owns an internal workspace and any number of client workspaces. Person is organization-level; OrganizationMembership and WorkspaceMembership grant roles without duplicating a human per client. Actor remains a scoped identity for legacy API and agent evidence access.

Delivery hierarchy:

    Client workspace → Project → Campaign → WorkItem → Deliverable → Review

WorkItem retains the forced captured, assigned, in_progress, review, client_review, shipped flow and adds hierarchy, owner/assignee/reviewer, watchers, tags, estimates, actual effort, dependencies, blocking reason, files, links, comments, versions, brief, brain context, time entries, and financial value.

Meetings and communication create Signals. Signals route to work, risk, decision, notification, approval, brain, or a proposal. Uncertain information stays proposed.

A client workspace can also grant an external contact the "client" workspace
role. That role has no general write capability; a client-portal ClientIntakeRequest
is the only front door for client-originated work, and it must be explicitly
accepted by staff before it becomes a canonical WorkItem. Client-portal
identities can additionally comment on and decide `kind="client"` Reviews for
their own workspace only.

The remaining domains are client health, risks, opportunities, contracts/scope, contacts/relationships, campaigns, creative, content, people/capacity, finance, agents, automations, integrations, reports, notifications, and approvals.
