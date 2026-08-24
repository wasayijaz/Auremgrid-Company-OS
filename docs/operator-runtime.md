# Operator runtime

Run the web process privately (`127.0.0.1`) and the durable worker as separate
processes against the same database. The worker claims one job at a time with a
lease and fencing token; an expired lease is recoverable by a later worker.

`python -m auremgrid worker-loop --db <path> --organization <id> --worker-id <id>`
is the packaged loop. Use `scripts/worker-loop.ps1` on Windows or
`scripts/worker-loop.sh` under a Linux supervisor. Keep the database and
`AUREMGRID_DEPLOYMENT_KEY` private; neither is accepted in URLs or job payloads.

`GET /operator/health?worker_id=<id>` reports heartbeat, pause state, and the
last redacted result. Pause is an operator control in the durable scheduler
state; recovery mode and outbound dispatch controls remain separate gates.

For local readiness reviews, `CompanyOS.operator_readiness` exposes a read-only
admin service. `supervised_operations_export(...)` returns a bounded, redacted
packet covering scheduler heartbeats, agent runs, traces, tool calls, approved
action executions, automation definitions/runs, jobs, and evaluation circuit
events. `readiness_report(...)` turns that packet into operator checks without
mutating work. `postgres_portability_assessment()` is intentionally a dry
assessment: it opens no provider connection and reports the current SQLite-only
runtime, migration dialect, FTS, trigger, id generation, catalog, and transaction
blockers that must be resolved before a PostgreSQL deployment can be called
ready.

Agent command-center reads expose the exact supervised reversible action
catalog enforced by the executor. Agent run detail also returns approved action
execution rows with decoded result/error state and a replay boundary, so an
operator can distinguish completed idempotent replay, active fenced execution,
and failed same-key execution that needs a new approved task or idempotency key.
Automation status `active` with policy `auto` only permits the same allowlisted
reversible local actions enforced by that catalog. Unsafe, external, arbitrary
connector-write, or one-way actions remain human-gated and are not queued for
automatic execution.

The compose template binds the stdlib server only to loopback and puts TLS at
the reverse proxy. Copy `.env.example` to `deploy/.env`, replace
`AUREMGRID_DOMAIN`, and protect that file before deployment; `deploy/.env` is
the path used by `docker compose --env-file deploy/.env -f
deploy/docker-compose.yml ...`. Do not expose port 8791 directly. The template
is an operator-owned proxy/TLS pattern, not managed hosting. Operators remain
responsible for certificate issuance, firewall rules, per-person session
provisioning, backup/restore rehearsal, and secret rotation.

`scripts/prepare-deploy.ps1` is a readiness check only. It does not create
cron entries, Task Scheduler entries, systemd timers, services, or external
credentials. Operators install the backup schedule themselves and verify it by
listing the scheduler entry and confirming the command runs both `backup` and
`verify-backup`; after the first run, verify the produced backup again with
`auremgrid verify-backup --backup <backup-file>`.

First-run setup is CSV-first after the owner login exists. Use
`import-templates`, `import-preview`, and `import-commit` for client
workspaces, campaigns, and campaign metrics. Preview records durable batches
and quarantines invalid rows; commit creates canonical records only from valid
preview rows and writes a separate idempotent receipt. Spreadsheet users should
export CSV and paste/pass that CSV content; uploads do not accept arbitrary
local file paths.

Portal report delivery is portal-only. Staff publish completed report runs only
after a matching approved `report.portal_publish` request; clients then read or
download immutable snapshots through client-portal routes. No packaged worker
emails reports or performs an external provider send.
