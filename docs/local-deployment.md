# Local deployment

Use Python 3.12 or later. The default path needs no third-party package, network service, Docker runtime, API key, or frontend build.

Run tests with `.\tools\test.ps1`. Start the server with `PYTHONPATH` set to
`src` and run `python -m auremgrid.cli serve --host 127.0.0.1 --port 8791 --db
auremgrid-demo.sqlite --seed`. Use `bootstrap-auth` for an existing owner, then
enter the one-time session token when the dashboard opens. JSON endpoints do
not trust query/body person or actor identifiers.

Keep the SQLite database on durable storage. Use the online `backup` and
`verify-backup` commands before upgrades; do not copy a live WAL file. Run
durable jobs with a separate `worker-once` process. Secrets belong in an
external environment or secret manager and enter SQLite only as references and
fingerprints, never values.

## Optional local semantic model

The default installation uses the deterministic offline provider and needs no
model package. To opt into a model already stored on this machine, install the
optional package with `pip install -e ".[semantic]"` and provide all three
identity fields:

```powershell
python -m auremgrid.cli serve --db auremgrid.sqlite `
  --semantic-model-path D:\models\local-mini `
  --semantic-model local-mini `
  --semantic-version weights-2026-08-01
```

For persistent local process configuration, set
`AUREMGRID_SEMANTIC_MODEL_PATH`, `AUREMGRID_SEMANTIC_MODEL`, and
`AUREMGRID_SEMANTIC_VERSION`. Apply the same values to the server and workers.
The path must exist before use and the loader sets `local_files_only=True`; no
model download is attempted. Missing files, a missing optional dependency, or
a model load error report semantic health as degraded while startup, canonical
records, and full-text retrieval continue working. Changing model version or
dimensions causes the rebuildable vector projection to be regenerated; it does
not require a schema migration.
