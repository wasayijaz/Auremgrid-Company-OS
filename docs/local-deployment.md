# Local deployment

Use Python 3.12 or later. The default path needs no third-party package, network service, Docker runtime, API key, or frontend build.

Run tests with .\tools\test.ps1. Start the server with PYTHONPATH set to src and run python -m auremgrid.cli serve --host 127.0.0.1 --port 8791 --db auremgrid-demo.sqlite --seed.

Keep the SQLite database on durable storage and back it up before upgrades. Secrets belong in the provider environment or secret manager, never SQLite source content or repository fixtures.
