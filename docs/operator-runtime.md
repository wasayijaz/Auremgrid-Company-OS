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

The compose template binds the stdlib server only to loopback and puts TLS at
the reverse proxy. Replace `AUREMGRID_DOMAIN` and provide a protected `.env`
before deployment; do not expose port 8791 directly. The template is an
operator-owned proxy/TLS pattern, not managed hosting. Operators remain
responsible for certificate issuance, firewall rules, per-person session
provisioning, backup/restore rehearsal, and secret rotation.

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
