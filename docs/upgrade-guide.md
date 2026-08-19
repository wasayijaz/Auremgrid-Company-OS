# Upgrade guide

Opening a database runs ordered, append-only schema migrations recorded in schema_migrations. Current schema version is 10.

Before upgrading:

1. Stop writers.
2. Back up the SQLite file.
3. Run the full test suite against a copy.
4. Start Auremgrid; migrations apply in order.
5. Check /health for the schema version.
6. Rebuild local projections with CompanyOS.rebuild_projections when required.

Migrations preserve canonical rows. Obsolete API compatibility layers are not retained. Projections are disposable and rebuilt from canonical documents, facts, memories, and decisions.

