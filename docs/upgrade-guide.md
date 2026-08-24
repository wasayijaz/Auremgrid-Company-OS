# Upgrade guide

Opening a database runs ordered, append-only schema migrations recorded in
`schema_migrations`. The current schema version is **48** (`approved_reversible_action_executions`).

Before upgrading:

1. Stop writers.
2. Run `auremgrid backup` and `verify-backup`; do not copy a live WAL file.
3. Run the full test suite against a copy.
4. Start Auremgrid; migrations apply in order.
5. Check `/health` (or `/health/detailed`) for the schema version; a healthy
   current installation reports `48`.
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
durable specialist review results. Schema 47 adds supervised reversible action
descriptor fields to agent tasks and records the orchestration trace that
proposed them when available; replay is safe for older physical schemas that
already carried an action descriptor column. Schema 48 adds the
`agent_action_executions` ledger for approved reversible action execution,
with a scoped idempotency key, descriptor and payload hashes, status/result/error
fields, run linkage, and an append-only no-delete boundary.

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
`create_notification` (`notification.create`), and `acknowledge_attention`
(`proactive_attention.acknowledge`). Execution requires a safe, non-one-way
descriptor, an approved same-scope approval request with matching action kind
and canonical payload, and a scoped idempotency key. Rejected or otherwise
non-approved approvals block new execution; completed execution attempts remain
ledgered. External-send, connector-write, one-way, and arbitrary descriptors are
rejected.

