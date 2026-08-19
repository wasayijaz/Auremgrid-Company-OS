# Upgrade guide

Opening a database runs ordered, append-only schema migrations recorded in schema_migrations. Current schema version is 11.

Before upgrading:

1. Stop writers.
2. Run `auremgrid backup` and `verify-backup`; do not copy a live WAL file.
3. Run the full test suite against a copy.
4. Start Auremgrid; migrations apply in order.
5. Check /health for the schema version.
6. Rebuild local projections with CompanyOS.rebuild_projections when required.

Migrations preserve canonical rows. Obsolete API compatibility layers are not retained. Projections are disposable and rebuilt from canonical documents, facts, memories, and decisions.

Schema 11 makes JSON APIs authenticated. After migration, use the local
`bootstrap-auth` command for an existing organization owner and bind each
legacy evidence workspace actor that principal may use. A restore enters
recovery mode and revokes sessions, so issue a new session only after reviewing
pending jobs, approvals, and outbound state.

