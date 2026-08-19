# First serious agency release verification

This matrix maps the product definition of complete to authoritative implementation evidence.

| Requirement | Evidence |
|---|---|
| 1. Create an organization | OrganizationDeliveryTests and organizations migration |
| 2. Multiple clients under it | OrganizationDeliveryTests and DashboardService portfolio |
| 3. Team members span clients | test_one_person_spans_multiple_client_workspaces |
| 4. Client data remains isolated | cross-workspace evidence, project, relationship, connector, REST, and inference-leakage tests |
| 5. Client brains retain temporal truth and provenance | temporal supersession, conflict, citation, source ACL, and prompt-injection tests |
| 6. Projects and work run end to end | project/deliverable vertical slice, forced work loop, expanded hierarchy, dependencies, versions, and time tests |
| 7. Review and approvals are enforced | review lifecycle, timestamped comments, single-decision, and approval authorization tests |
| 8. Meetings and communication generate signals | client-operations, relationship, and promotion/sync tests |
| 9. Decisions are first class | Decision model/repository/API/MCP and human proposal-promotion tests |
| 10. Client health is explainable | unanswered-message, risk, overdue-work, and scope explanations in ClientOperationsTests |
| 11. Risks and opportunities use real signals | signal routing and scope-overage behavior tests |
| 12. Scope usage is tracked | contract, allowance, usage percentage, risk, and opportunity test |
| 13. Finance connects without fabricated values | not_connected and sourced finance record tests |
| 14. Campaigns and creatives are structured | campaign metric, creative library, content pipeline, performance schemas and tests |
| 15. Agents have scoped permissions and auditable runs | AgentAutomationTests and automatic ledger audit tests |
| 16. Automations trigger safely | training checkpoint, approved execution, outcome, and activation tests |
| 17. MCP/API cover major domains | ExpandedApiTests and namespaced MCP discovery test |
| 18. Dashboard surfaces major operations | 18 destinations including workflows, 8 metrics, Client HQ operational tabs, bearer-authenticated data fetches, operational Brain proposal/conflict/current-truth board, workflow stage board, degraded/loading/empty states, and dashboard/API behavior tests |
| 19. Projections rebuild after restart | ProjectionRestartTests, durable schema-16 embedding projection, and live rebuild report |
| 20. Tests prove isolation, persistence, permissions, and workflows | 314 offline behavior tests |
| 21. README accurately describes reality | implemented/local fallback/optional/experimental/planned status sections |
| 22. Existing data migrates forward | legacy-v1 migration test and schema 18 migration chain, including durable provider task identity, embedding projection, graph generations, entity resolution, and knowledge states |
| 23. Wings coordinate through executable operating contracts | eight neutral templates, immutable definition versions/run snapshots, dependency and rework behavior, evidence/approval/handoff gates, REST/MCP parity, dashboard status, and workflow isolation tests |
| 24. Repository changes are continuously verified | read-only GitHub Actions workflow compiles source/tests and runs the complete unittest suite on pushes and pull requests |
| 25. Public callers cannot impersonate people or actors | bearer-derived principals, actor bindings, 401/403 separation, REST forgery tests, and MCP identity-parity tests |
| 26. Background work survives process failure | durable principal-scoped jobs, atomic leases, fencing, progress, retry/dead-letter, cancellation, idempotency, append-only events, and restart worker test |
| 27. Credentials are not stored in operational records | hash-only auth tokens, external secret references, runtime resolution, redaction, and raw-database sentinel tests |
| 28. Backups are verifiable and restores are fenced | online backup API, checksum/quick/FK checks, manifests, atomic restore, session revocation, recovery mode, outbound disable, and projection rebuild test |
| 29. Enabled live read synchronization is restart-safe and honest | Slack/ClickUp/Google Drive/Gmail account and permission verification, explicit workspace mappings, durable provider cursors/inbox/dedupe, baseline-first backfill, Drive reconciliation/descendant tasks, Gmail label lifecycle, opaque cross-workspace quarantine, canonical ingestion, provider-aware retry timing, raw-DB secret scans, and no caller-controlled connected state |

Release checks:

- tools/test.ps1
- Python compileall over src and tests
- git diff --check
- live Playwright dashboard interaction
- on-disk schema and projection rebuild inspection
