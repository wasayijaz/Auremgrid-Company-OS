# Single-host performance baseline

`scripts/performance_baseline.py` is a deterministic rehearsal for agency
adoption sizes of 10, 25, and 50 client workspaces. It uses the existing
CompanyOS services with in-memory SQLite and synthetic, isolated records; it
does not contact providers or start a browser/HTTP server. Each repeated
service path is warmed once and then timed (default: three samples). One-shot
operations report elapsed time and their operational counters.

The rehearsal covers the V1 single-host paths: dashboard command, Brain
search, Intelligence workspace and portfolio projections, proactive
Intelligence refresh, workflow board query, large work-list read, projection
rebuild, online backup and verification, migration/open, and local worker queue
throughput.

Run it with:

```text
.venv\\Scripts\\python.exe scripts/performance_baseline.py
```

Reference run (Windows, schema 59, two timed samples, 2026-08-31):

| Clients | Dashboard (ms) | Brain search (ms) | Intel workspace (ms) | Intel portfolio (ms) | Proactive refresh (ms) | Workflow query (ms) | Large work list (ms) | Projection rebuild (ms) | Backup + verify (ms) | Migration/open (ms) | Worker jobs/sec |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 10 | 9.083 | 1.028 | 1.461 | 7.491 | 3.596 | 0.049 | 1.003 | 5.273 | 194.809 | 44.308 | 551.886 |
| 25 | 7.274 | 0.669 | 1.285 | 11.415 | 2.727 | 0.033 | 1.060 | 7.792 | 175.198 | 62.820 | 626.227 |
| 50 | 8.605 | 0.577 | 1.799 | 19.775 | 3.592 | 0.025 | 1.865 | 14.551 | 108.480 | 131.736 | 624.875 |

The rehearsal completed in 2.74 seconds. Backup sizes were 3,309,568 bytes
for 10 clients, 3,469,312 bytes for 25 clients, and 3,751,936 bytes for 50
clients. Worker throughput drained one local proactive-refresh job per client
and all jobs succeeded in the reference run.

These are service-level local baselines, not production SLOs: browser
rendering, network/auth overhead, concurrent operators, disk type, and external
connectors are intentionally not measured. The workflow fixture includes a
roster and one local workflow run, but does not model a large active delivery
board. Re-run on the deployment host and retain the JSON output as the release
artifact when hardware or schema changes.
