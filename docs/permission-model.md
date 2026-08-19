# Permission model

Authorization is layered:

1. Organization membership establishes company access.
2. Workspace membership establishes client access and admin, operator, or viewer capability.
3. Actor roles protect the evidence and legacy work APIs.
4. Source allowed-actor lists protect restricted evidence.
5. Agents have explicit allowed workspaces, tools, memory access, and write permissions.
6. Approval policies gate sensitive or one-way actions.

ACL checks run before record lookup, aggregation, ranking, counts, and error detail. A denied caller must not learn whether a cross-client object exists.

Organization owner/admin may configure agents, integrations, team skills, and entity merges. Viewers cannot mutate workspace records. New automations start in training mode regardless of their future policy.
