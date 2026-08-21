# Permission model

Authorization is layered:

1. Organization membership establishes company access.
2. Workspace membership establishes client access and admin, operator, viewer, or client capability.
3. Actor roles protect the evidence and legacy work APIs.
4. Source allowed-actor lists protect restricted evidence.
5. Agents have explicit allowed workspaces, tools, memory access, and write permissions.
6. Approval policies gate sensitive or one-way actions.

ACL checks run before record lookup, aggregation, ranking, counts, and error detail. A denied caller must not learn whether a cross-client object exists.

Organization owner/admin may configure agents, integrations, team skills, and entity merges. Viewers cannot mutate workspace records. New automations start in training mode regardless of their future policy.

## Client portal

The "client" workspace role is for external client contacts, not staff. It
never carries `workspace_write`; it only carries `workspace_read` and the
narrow `client_portal` capability. That capability is the sole path a client
identity has to write anything, and every write it allows is workspace-scoped
and re-validated against that person's own `client`-role membership row, so
a client identity cannot act on a workspace it does not belong to even if it
learns that workspace's id.

A client can only:

- submit an intake request into a workspace's pending queue (never create a
  `WorkItem` directly -- intake requires an explicit staff accept/decline);
- comment on and decide `kind="client"` reviews for their own workspace
  (`kind="internal"` reviews are unreachable from the client-portal surface).

Staff accept/decline of an intake request requires the `people_manage`
capability, the same capability workspace membership changes already require.
