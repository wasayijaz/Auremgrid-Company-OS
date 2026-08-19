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
