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
| 18. Dashboard surfaces major operations | browser verification: 17 destinations, 8 metrics, 3 attention items, 15 Client HQ tabs, live work/people/module data, zero console errors |
| 19. Projections rebuild after restart | ProjectionRestartTests and live rebuild report |
| 20. Tests prove isolation, persistence, permissions, and workflows | 79 offline behavior tests |
| 21. README accurately describes reality | implemented/local fallback/optional/experimental/planned status sections |
| 22. Existing data migrates forward | legacy-v1 migration test and migrated auremgrid-demo.sqlite at schema 9 |

Release checks:

- tools/test.ps1
- Python compileall over src and tests
- git diff --check
- live Playwright dashboard interaction
- on-disk schema and projection rebuild inspection
