# First serious agency release verification

This matrix maps the product definition of complete to authoritative implementation evidence.

| Requirement | Evidence |
|---|---|
| 1. Create an organization | OrganizationDeliveryTests and organizations migration |
| 2. Multiple clients under it | OrganizationDeliveryTests and DashboardService portfolio |
| 3. Team members span clients | test_one_person_spans_multiple_client_workspaces |
| 4. Client data remains isolated | cross-workspace evidence, project, relationship, connector, REST, and inference-leakage tests |
| 5. Client brains retain temporal truth and provenance | bitemporal recording/effective-time fences, temporal supersession, conflict, citation, source ACL, timezone validation, and prompt-injection tests |
| 6. Projects and work run end to end | project/deliverable vertical slice, forced work loop, expanded hierarchy, dependencies, versions, and time tests |
| 7. Review and approvals are enforced | review lifecycle, timestamped comments, single-decision, and approval authorization tests |
| 8. Meetings and communication generate signals | client-operations, relationship, and promotion/sync tests |
| 9. Decisions are first class | Decision model/repository/API/MCP and human proposal-promotion tests |
| 10. Client health is explainable | Pure `explain_health` read model plus explicit snapshot calculation, component scores, explanations, evidence references, authenticated HTTP, Client HQ Health UI, and P10-P12 tests |
| 11. Risks and opportunities use real signals | Signal routing plus create/resolve/reopen risk and create/advance/close opportunity lifecycles, append-only event histories/audits, authenticated HTTP, Client HQ controls, isolation, and invalid-transition tests |
| 12. Scope usage is tracked | Contract/allowance/quantity-or-hours usage, period history, explicit no-data/recorded/over-scope states, generated risk/opportunity links, authenticated HTTP, and Client HQ Scope UI |
| 13. Finance connects without fabricated values | Explicit disconnected state plus sourced revenues, invoices, budgets, labor/other/software/AI costs, client contribution/margin calculation, authenticated routes, and focused economics tests |
| 14. Campaigns and creatives are structured | Legal campaign transitions, append-only lifecycle audits, campaign metrics, immutable creative versions, reviewer-gated approval/revision flow, sourced creative performance, authenticated inspectors/actions, and focused lifecycle tests |
| 15. Agents have scoped permissions and auditable runs | Scoped queue claim, workspace-fenced tool calls, ordered traces, outputs/errors/costs, person-visible run list/detail, capability-level routing, authenticated routes, command-center inspectors, and observability tests |
| 16. Automations trigger safely | training checkpoint, approved execution, outcome, and activation tests |
| 17. MCP/API cover major domains | ExpandedApiTests and namespaced MCP discovery test |
| 18. Dashboard surfaces major operations | 18 destinations including workflows, 8 metrics, an accountable Client HQ roster/meeting/workload/readiness view, a derived weekly capacity view, bearer-authenticated data fetches, operational Brain proposal/conflict/current-truth board plus evidence-backed entity discovery, workflow stage board, capability-gated descriptor actions with idempotency/expected-version payloads, historical zero-action behavior, degraded/loading/empty states, and dashboard/API behavior tests |
| 19. Projections rebuild after restart | ProjectionRestartTests, durable schema-16 embedding projection, fenced schema-17 graph generations, append-only schema-21 Graphiti episode-key/provider-UUID mappings, opt-in local-only SentenceTransformers identity/version fencing, and live rebuild report |
| 20. Tests prove isolation, persistence, permissions, and workflows | Full offline behavior suite plus a deterministic ten-scenario Chromium dashboard gate |
| 21. README accurately describes reality | implemented/local fallback/optional/experimental/planned status sections |
| 22. Existing data migrates forward | legacy-v1 migration test and schema 40 migration chain, including durable provider task identity, embedding projection, graph generations and Graphiti UUID sidecar, entity resolution, knowledge states, existing-agent level backfill, client account rosters, CSV import previews/receipts, brain customization versions, provider import ledgers, portal report versions/events, and scheduler health records |
| 23. Wings coordinate through executable operating contracts | eight neutral templates, immutable definition versions/run snapshots, dependency and rework behavior, evidence/approval/handoff gates, REST/MCP parity, dashboard status, and workflow isolation tests |
| 24. Repository changes are continuously verified | read-only GitHub Actions workflow compiles source/tests and runs the complete unittest suite on pushes and pull requests |
| 25. Public callers cannot impersonate people or actors | bearer-derived principals, actor bindings, 401/403 separation, REST forgery tests, and MCP identity-parity tests |
| 26. Background work survives process failure | durable principal-scoped jobs, atomic leases, fencing, progress, retry/dead-letter, cancellation, idempotency, append-only events, restart worker test, and on-disk Google backfill close/reopen proof |
| 27. Credentials are not stored in operational records | hash-only auth tokens, external secret references, runtime resolution, redaction, and raw-database sentinel tests |
| 28. Backups are verifiable and restores are fenced | online backup API, checksum/quick/FK checks, manifests, atomic restore, session revocation, recovery mode, outbound disable, and projection rebuild test |
| 29. Enabled live read synchronization is restart-safe and honest | Slack/ClickUp/Google Drive/Gmail/Figma/Fireflies account and permission verification, explicit workspace mappings, durable provider cursors/inbox/dedupe, baseline-first backfill, Drive reconciliation/descendant tasks, Gmail label lifecycle, version-fenced exact-file Figma polling/tombstones, bounded frame/section/comment evidence, and optional bounded named-version evidence with the parent file as the only lifecycle/object-count record; single-account Fireflies transcript polling with sanitized bounded transcript evidence and no delete/tombstone signal; no Figma model review or approval workflows/auto-created deliverables, reviews, or tasks; opaque cross-workspace quarantine, verified read-connector evidence ingestion, provider-aware retry timing, raw-DB secret scans, and no caller-controlled connected state |
| 30. Brain retrieval is operable and explainable offline | dependency-free lexical fallback and local graph by default, opt-in local-only SentenceTransformers and explicitly configured Graphiti/Neo4j projection, full-workspace ACL gating before upstream lookup, canonical rehydration, bitemporal eligibility before scoring, bounded hybrid relevance/source-authority/recency contributions, deterministic ties, sanitized unavailable/degraded health, and effective knowledge state across REST/MCP/dashboard reads |
| 31. Intelligence reasoning remains bounded and verifiable | `evaluate-intelligence` runs offline scenarios for ACL-scoped citations, uncertainty, structured reasoning, approval descriptors, no unauthorized actions, and malformed-provider deterministic fallback; see [AutoGPT adoption decision](autogpt-adoption.md) for the clean-room licensing and architecture boundary |

Additional capability batch: feedback_patterns, performance_insights, forecasts,
retention_policies, CSV-first onboarding imports, brain customizations,
portal-only report publication, durable scheduler health, and injected
read-only Stripe Billing/accounting and Meta Ads provider import ledgers are
guarded as newer authenticated service/route surfaces outside the P6-P15
release matrix. Provider import ledgers are raw/provider append-only records
with cursor and quarantine state; they are not documented here as verified
canonical finance or campaign ingestion.

Release checks:

- tools/test.ps1
- Python compileall over src and tests
- python scripts/dashboard_showcase_svg.py
- git diff --check
- `python -m auremgrid.cli evaluate-intelligence`
- `tools/run-dashboard-browser.ps1` (deterministic Playwright/Chromium gate)
- on-disk schema and projection rebuild inspection

Clean-room walkthrough evidence should be regenerated for each release. The
historic 2026-08-19 walkthrough covered the zero-install launcher, `demo`,
`bootstrap-auth`, `serve`, `/health`, authenticated `/dashboard/brain`,
authenticated `/dashboard/workflows`, `worker-once`, `backup`, and
`verify-backup` against the then-current schema 18 database. Do not reuse that
historic schema/version count as current release evidence; current release
verification must report schema 40 from `/health` or `/health/detailed`.
