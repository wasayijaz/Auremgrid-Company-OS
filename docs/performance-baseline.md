# Single-host performance baseline

`scripts/performance_baseline.py` is a deterministic rehearsal for agency
adoption sizes of 10, 25, and 50 client workspaces. It uses the existing
CompanyOS services with in-memory SQLite and synthetic, isolated records; it
does not contact providers or start a browser/HTTP server. Each read path is
warmed once and then timed (default: three samples), while backup timing covers
an online SQLite backup plus checksum, quick-check, and foreign-key verification.

Run it with:

```text
.venv\\Scripts\\python.exe scripts/performance_baseline.py
```

Reference run (Windows, schema 58, two timed samples, 2026-08-31):

| Clients | Dashboard command (ms) | Brain search (ms) | Work list (ms) | Workflow query (ms) | Intelligence brief (ms) | Backup + verify (ms) |
|---:|---:|---:|---:|---:|---:|---:|
| 10 | 3.555 | 0.322 | 0.029 | 0.017 | 1.756 | 72.732 |
| 25 | 4.075 | 0.485 | 0.044 | 0.021 | 5.785 | 111.271 |
| 50 | 4.732 | 0.453 | 0.034 | 0.018 | 8.450 | 91.658 |

The rehearsal completed in 0.98 seconds. These are service-level local
baselines, not production SLOs: browser rendering, network/auth overhead,
concurrent operators, disk type, and external connectors are intentionally not
measured. The workflow fixture includes a roster and one local workflow run,
but does not model a large active delivery board. Re-run on the deployment host
and retain the JSON output as the release artifact when hardware or schema
changes.
