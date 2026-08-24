# Upgrade guide

Opening a database runs ordered, append-only schema migrations recorded in
`schema_migrations`. The current documented release-line schema version is
**58** (`webhook_quarantine_receipts`); `/health` or `/health/detailed` is the
authoritative runtime evidence for the exact artifact being upgraded.

Before upgrading:

1. Stop writers.
2. Run `auremgrid backup` and `verify-backup`; do not copy a live WAL file.
3. Run the full test suite against a copy.
4. Start Auremgrid; migrations apply in order.
5. Check `/health` (or `/health/detailed`) for the schema version; a healthy
   schema-58 release-line installation reports `58`, and later artifacts must
   report their shipped schema version.
6. Rebuild local projections with CompanyOS.rebuild_projections when required.

Migrations preserve canonical rows. Obsolete API compatibility layers are not
retained. Projections are disposable and rebuilt from canonical documents,
facts, memories, decisions, and sourced operating records.

The current forward-only chain includes authenticated JSON APIs and jobs,
connector inbox/cursors and quarantine, semantic and graph projections,
entity-resolution and knowledge-state events, agent levels and routing, client
account rosters, portal intake, feedback/performance insights, forecasts,
retention/deletion, secure provider references, backup/recovery, rich review
annotations, proactive Intelligence snapshots, work-transition idempotency,
client/campaign lifecycle events, the revenue-operations tables for prospects,
proposals, conversions, pacing signals, retainer reads, internal report-pack
approvals/delivery history, append-only onboarding CSV import batches, rows,
errors, and receipts, brain-customization controls, read-only provider import
records for injected Stripe Billing/accounting and Meta Ads adapters, approved
portal-only client report versions/events, scoped scheduler/operator heartbeat
and pause-state records, richer provider-import quarantine details, immutable
Intelligence expert profiles/runbooks, append-only Intelligence hypotheses and
recommendation lifecycle records, shadow-only Intelligence evaluation/circuit
breaker records, and proactive Intelligence attention lifecycle state.
Schema 46 adds append-only persisted Intelligence orchestration traces for
durable specialist review results. Schemas 47 and 48 add supervised reversible
action descriptor fields and the `agent_action_executions` ledger for approved
reversible action execution, with scoped idempotency, descriptor/payload hashes,
run linkage, and an append-only no-delete boundary. Schemas 49 through 53 add
Intelligence contract audit hooks, recommendation handoffs, local admin auth
invites, Google Ads provider import records, and CRM provider import records.
Schema 54 adds durable automation action execution records, replay-safe run
fingerprints, and bounded delegated task depth.
Schema 55 adds durable Hypothesis subjects and lifecycle timestamps. Schema 56
adds principal-aware workflow roster ownership and append-only proposed meeting
output routes.
Schema 57 adds outbound-send attempt fencing, asset backup manifest links, and
review-media contracts. Schema 58 adds digest-only webhook quarantine receipts.

Schema 11-era databases also need the authenticated identity bootstrap. After
migration, use the local `bootstrap-auth` command for an existing organization
owner and bind each legacy evidence workspace actor that principal may use. A
restore enters recovery mode and revokes sessions, so issue a new session only
after reviewing pending jobs, approvals, and outbound state. Finance rows are
still connected-only and source-required after upgrade; no migration fabricates
revenue, invoices, costs, budgets, software/AI costs, or client economics.
Portal reports remain snapshots of completed report runs and require an
approved `report.portal_publish` request before becoming visible to client
portal identities; no migration creates sends or external delivery.
Intelligence migrations add contracts, learning, evaluation safety, and
proactive lifecycle/orchestration/action-descriptor records only; they do not
promote hypotheses into facts or change agent routing. Descriptor execution is
limited to the exact supervised catalog: `generate_report` (`report.generate`),
`create_notification` (`notification.create`), `acknowledge_attention`
(`proactive_attention.acknowledge`), `create_risk` (`risk.create`),
`add_work_comment` (`work.comment.create`), and `create_proposal`
(`brain.proposal.create`). Execution requires a safe, non-one-way descriptor,
an approved same-scope approval request with matching action kind and canonical
payload, and a scoped idempotency key. Rejected or otherwise non-approved
approvals block new execution; completed execution attempts remain ledgered.
Failed same-key attempts are fenced until an operator creates a new approved
task or idempotency key. External-send, connector-write, one-way, and arbitrary
descriptors are rejected.

