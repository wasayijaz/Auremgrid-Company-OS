# Upgrade guide

Opening a database runs ordered, append-only schema migrations recorded in
`schema_migrations`. The current schema version is **35** (`agency_revenue_operations`).

Before upgrading:

1. Stop writers.
2. Run `auremgrid backup` and `verify-backup`; do not copy a live WAL file.
3. Run the full test suite against a copy.
4. Start Auremgrid; migrations apply in order.
5. Check `/health` (or `/health/detailed`) for the schema version; a healthy
   current installation reports `35`.
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
client/campaign lifecycle events, and the revenue-operations tables for
prospects, proposals, conversions, pacing signals, retainer reads, and
internal report-pack approvals/delivery history.

Schema 11-era databases also need the authenticated identity bootstrap. After
migration, use the local `bootstrap-auth` command for an existing organization
owner and bind each legacy evidence workspace actor that principal may use. A
restore enters recovery mode and revokes sessions, so issue a new session only
after reviewing pending jobs, approvals, and outbound state. Finance rows are
still connected-only and source-required after upgrade; no migration fabricates
revenue, invoices, costs, budgets, software/AI costs, or client economics.

