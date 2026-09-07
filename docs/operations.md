# Auremgrid Company OS — Operations Manual

This document is the factual, reference-grade operator guide for running, maintaining, backing up, restoring, and troubleshooting Auremgrid Company OS on private hosts. Every command, flag, and configuration parameter corresponds to real implementations in `src/auremgrid/cli.py`, `src/auremgrid/storage/backup.py`, and `scripts/`.

---

## 1. System Requirements & Architecture

- **Operating System**: Windows (PowerShell) or Linux/macOS.
- **Python Runtime**: Python 3.12 or newer. Use `.venv\Scripts\python.exe` (Windows) or `.venv/bin/python` (POSIX).
- **Storage**: SQLite 3 (WAL mode enabled by default in `SqliteStore`).
- **Dependencies**: Bundled dependencies in local virtual environment; no external Redis, message broker, or public cloud dependency required.
- **Network Interfaces**: Binds to loopback (`127.0.0.1`) by default. For private multi-client access, front with an operator-managed reverse proxy (e.g. Nginx or Caddy) handling TLS termination and certificate management.

---

## 2. Initial Setup & Agency Provisioning

### 2.1 First-Run Setup (`setup-agency`)

To provision a new agency, initial owner account, default workspace, and issue the first dashboard authentication token in a single command:

```powershell
.venv\Scripts\python.exe scripts\auremgrid.py setup-agency `
  --agency "Acme Media" `
  --admin-name "Jane Doe" `
  --admin-email "jane@acmemedia.example" `
  --db "data\auremgrid.sqlite" `
  --dashboard-url "http://127.0.0.1:8787/"
```

The command outputs a JSON receipt containing:
- `agency.id`: Generated or requested organization identifier (e.g. `org_acme_media`).
- `workspace.id`: Generated client workspace identifier (e.g. `ws_acme_media`).
- `owner.id`: Generated owner person identifier.
- `session.token`: Ephemeral session token (shown once; keep private).

### 2.2 Dedicated Workspace Onboarding (`onboard`)

To add an additional isolated agency or client workspace to an existing database:

```powershell
.venv\Scripts\python.exe scripts\auremgrid.py onboard `
  --agency "Apex Partners" `
  --workspace "ws_apex_client" `
  --admin "Apex Admin" `
  --operator "Apex Operator" `
  --db "data\auremgrid.sqlite"
```

### 2.3 Business Data Import Pipeline (CSV-First)

Initial business records (client workspaces, campaigns, and metrics) are imported via a three-stage dry-run pipeline:

1. **Generate Templates**:
   ```powershell
   .venv\Scripts\python.exe scripts\auremgrid.py import-templates --db "data\auremgrid.sqlite"
   ```

2. **Validate & Preview Import** (piping CSV data via standard input):
   ```powershell
   Get-Content "campaigns.csv" | .venv\Scripts\python.exe scripts\auremgrid.py import-preview `
     --organization "org_acme_media" `
     --workspace "ws_acme_media" `
     --person "person_jane" `
     --type "campaigns" `
     --idempotency-key "import_batch_2026_09_01" `
     --db "data\auremgrid.sqlite"
   ```

3. **Commit Validated Batch**:
   ```powershell
   .venv\Scripts\python.exe scripts\auremgrid.py import-commit `
     --organization "org_acme_media" `
     --batch "batch_id_from_preview" `
     --person "person_jane" `
     --idempotency-key "commit_batch_2026_09_01" `
     --db "data\auremgrid.sqlite"
   ```

---

## 3. Serving & Background Processing

### 3.1 Web & Dashboard Server (`serve`)

Start the local HTTP API and embedded operator dashboard:

```powershell
.venv\Scripts\python.exe scripts\auremgrid.py serve `
  --db "data\auremgrid.sqlite" `
  --host "127.0.0.1" `
  --port 8787
```

- `--seed`: Automatically seed the synthetic demo fixtures before starting.
- `--storage`: Defaults to `sqlite`. (`postgres` requires `--postgres-url`).

### 3.2 Background Durable Workers

Durable background jobs (`report.generate`, `projection.rebuild`, `proactive_intelligence.refresh`, `agent.run`, `automation.execute`, `connector.sync`) are processed by worker instances.

- **Run Single Job** (useful for scheduled task triggers, cron, or testing):
  ```powershell
  .venv\Scripts\python.exe scripts\auremgrid.py worker-once `
    --db "data\auremgrid.sqlite" `
    --organization "org_acme_media" `
    --worker-id "worker-node-1"
  ```

- **Run Continuous Worker Loop**:
  ```powershell
  .venv\Scripts\python.exe scripts\auremgrid.py worker-loop `
    --db "data\auremgrid.sqlite" `
    --organization "org_acme_media" `
    --worker-id "worker-node-1" `
    --poll-seconds 1.0
  ```

Alternatively, use the provided PowerShell loop runner:
```powershell
.\scripts\worker-loop.ps1 -DbPath "data\auremgrid.sqlite" -OrganizationId "org_acme_media"
```

---

## 4. Authentication & Session Management

Auremgrid uses a local principal, actor binding, and session token architecture. Tokens are passed in HTTP headers as `Authorization: Bearer <session_token>`.

### 4.1 Bootstrap Administrative Session (`bootstrap-auth`)

Create a local principal and issue a session token for an existing person:

```powershell
.venv\Scripts\python.exe scripts\auremgrid.py bootstrap-auth `
  --db "data\auremgrid.sqlite" `
  --organization "org_acme_media" `
  --person "person_jane" `
  --email "jane@acmemedia.example" `
  --workspace "ws_acme_media" `
  --actor "act_admin"
```

### 4.2 Local Invite Tokens

Local administrators can issue one-time invite tokens without requiring external SMTP:

```powershell
# Create an invite token
.venv\Scripts\python.exe scripts\auremgrid.py auth-invite-create `
  --db "data\auremgrid.sqlite" `
  --admin-token "<admin_session_token>" `
  --person "person_mark" `
  --email "mark@acmemedia.example" `
  --workspace "ws_acme_media" `
  --actor "act_operator" `
  --expires-in-seconds 604800

# Consume invite token to receive session
.venv\Scripts\python.exe scripts\auremgrid.py auth-invite-consume `
  --db "data\auremgrid.sqlite" `
  --admin-token "<admin_session_token>" `
  --invite-token "<invite_token>"
```

### 4.3 Inspect and Revoke Sessions

```powershell
# List active sessions
.venv\Scripts\python.exe scripts\auremgrid.py auth-sessions `
  --db "data\auremgrid.sqlite" `
  --admin-token "<admin_session_token>"

# Revoke a compromised or terminated session
.venv\Scripts\python.exe scripts\auremgrid.py auth-session-revoke `
  --db "data\auremgrid.sqlite" `
  --admin-token "<admin_session_token>" `
  --session-id "<session_id>"
```

---

## 5. Demonstration & Evaluation Scenarios

- **Basic Demo**: Seeds synthetic accounts and tests semantic search:
  ```powershell
  .venv\Scripts\python.exe scripts\auremgrid.py demo --db :memory:
  ```

- **Realistic Agency Demo**: Seeds a full agency scenario with 4 client workspaces, realistic rosters, creative versions, review events, and proactive attention items:
  ```powershell
  .venv\Scripts\python.exe scripts\auremgrid.py demo-agency `
    --db "data\agency_demo.sqlite" `
    --organization "org_demo" `
    --owner "person_demo_owner"
  ```

- **Print Account Brief**:
  ```powershell
  .venv\Scripts\python.exe scripts\auremgrid.py brief `
    --db "data\agency_demo.sqlite" `
    --workspace "ws_alpha" `
    --actor "act_alpha_operator" `
    --query "consultation price"
  ```

- **Run Intelligence Benchmark & Evaluation Contracts**:
  ```powershell
  .venv\Scripts\python.exe scripts\auremgrid.py evaluate-intelligence
  ```

---

## 6. Backup Operations

Auremgrid uses the SQLite Online Backup API (`connection.backup()`) to take consistent, point-in-time snapshots while the database is actively being written to.

### 6.1 Create Online Backup (`backup`)

```powershell
.venv\Scripts\python.exe scripts\auremgrid.py backup `
  --db "data\auremgrid.sqlite" `
  --output "data\backups\auremgrid-2026-09-07T120000Z.sqlite"
```

This creates two files:
1. `auremgrid-2026-09-07T120000Z.sqlite`: Consistent database snapshot.
2. `auremgrid-2026-09-07T120000Z.sqlite.manifest.json`: Manifest containing:
   - SHA-256 checksum of the backup file.
   - Schema version.
   - Total table count.
   - SQLite `PRAGMA quick_check` status (`ok`).
   - Foreign-key violation count (must be `0`).
   - Representative row counts (`organizations`, `workspaces`, `documents`, `workflow_runs`, `ledger_audit`).

### 6.2 Verify Existing Backup (`verify-backup`)

Verify that a backup file's SHA-256 matches its manifest and that internal tables and foreign keys are valid:

```powershell
.venv\Scripts\python.exe scripts\auremgrid.py verify-backup `
  --backup "data\backups\auremgrid-2026-09-07T120000Z.sqlite"
```

### 6.3 Database Integrity Check (`check-integrity`)

```powershell
.venv\Scripts\python.exe scripts\auremgrid.py check-integrity --db "data\auremgrid.sqlite"
```

### 6.4 Rotate Old Backups (`backup-rotate`)

Enforces retention policy (default: 7 daily backups, 4 weekly backups):

```powershell
.venv\Scripts\python.exe scripts\auremgrid.py backup-rotate `
  --dir "data\backups" `
  --keep-daily 7 `
  --keep-weekly 4
```

---

## 7. Restoration & Disaster Recovery

Restoration must be performed while the serving API is offline.

### 7.1 Execute Restore (`restore`)

```powershell
.venv\Scripts\python.exe scripts\auremgrid.py restore `
  --backup "data\backups\auremgrid-2026-09-07T120000Z.sqlite" `
  --db "data\auremgrid.sqlite" `
  --overwrite
```

### 7.2 Safety Invariants Enforced During Restore

When `restore_backup()` executes, the following safety actions occur automatically:

1. **Source Checksum & Integrity Validation**: The source backup is verified against its manifest and checked for corruption before any changes are made.
2. **Pre-Restore Safety Snapshot**: If the target `--db` destination already exists, a timestamped snapshot (`<name>.pre-restore-<timestamp>.sqlite`) is automatically created in the same directory prior to replacement.
3. **Session Invalidation**: All active `auth_sessions` and `api_tokens` have their `revoked_at` timestamp set to the restore time, preventing unauthorized session reuse.
4. **Job Queue Reset**: Any job previously in `leased` or `running` state is safely reset to `retry_wait` with all lease tokens cleared.
5. **Outbox Freeze**: All unpublished `outbox_events` have active lease owners and tokens cleared.
6. **Recovery Mode Activation**: Two keys are written to the `system_state` table:
   - `recovery_mode = '1'`: Signals that the node was recovered from a backup.
   - `outbound_dispatch = 'disabled'`: Hard fence preventing outbound webhooks or email dispatch until explicitly lifted by an operator.

### 7.3 Post-Restore Verification Checklist

After running `restore`, operators should:
1. Run `.\scripts\restore_drill.ps1` to verify restoration integrity.
2. Rebuild temporal and graph projections:
   ```powershell
   .venv\Scripts\python.exe -c "from auremgrid.services.brain import CompanyOS; os = CompanyOS('data/auremgrid.sqlite'); print(os.rebuild_projections())"
   ```
3. Issue fresh administrator credentials via `bootstrap-auth`.
4. Re-enable outbound dispatch once verified:
   ```powershell
   .venv\Scripts\python.exe -c "import sqlite3; conn = sqlite3.connect('data/auremgrid.sqlite'); conn.execute(\"UPDATE system_state SET value='enabled' WHERE key='outbound_dispatch'\"); conn.commit()"
   ```

---

## 8. Schema Upgrades & Forward Migration Rehearsal

Database migrations are tracked in `src/auremgrid/storage/migrations.py` and applied automatically on `CompanyOS` startup.

Before deploying a code update to a production host, rehearse forward migration against prior schema snapshots:

```powershell
.venv\Scripts\python.exe scripts\forward_migration_rehearsal.py
```

This script:
1. Builds a database at schema version `N-1`.
2. Runs `migrate()` forward to the latest version.
3. Validates `PRAGMA integrity_check` and checks schema version integrity.

---

## 9. Health & Observability Endpoints

The HTTP service exposes the following unauthenticated and authenticated monitoring paths:

| Endpoint | Method | Purpose | Key Return Fields |
|---|---|---|---|
| `/health` | GET | Lightweight readiness probe | `{"ok": true, "status": "healthy", "schema_version": 59}` |
| `/health/detailed` | GET | Deep subsystem audit | `{"ok": true, "status": "healthy", "integrity": "ok", "foreign_keys": 0, "warnings": []}` |
| `/dashboard/settings` | GET | Authenticated ledger and permission audit | Org view, operator identity, ledger health, capabilities |
| `/dashboard/workflows` | GET | Active workflow stages by state | `pending`, `in_progress`, `waiting_approval`, `completed` |

---

## 10. Audit, Logs, and Forensic Records

Auremgrid records all operations in durable SQLite tables:

- **`ledger_audit`**: Immutable append-only audit trail populated by SQLite database triggers. Contains timestamp, actor, action, table, record ID, and change summary.
- **`jobs`**: Queue of background tasks, worker lease owners, retry attempts, and failure logs.
- **`outbox_events`**: Durable external dispatch intents, payload hashes, approval linkages, and publication receipts.
- **`intelligence_orchestrator_runs`**: Execution traces of specialist deliberation, hypotheses, contradictions, and recommended actions.

---

## 11. Incident Response Checklist (Pre-Restart Capture)

If an operator encounters unexpected errors, deadlocks, or anomalies, **capture forensic evidence before restarting or killing processes**:

1. **Preserve Database Files**:
   ```powershell
   Copy-Item "data\auremgrid.sqlite" "data\incident_snapshot.sqlite"
   Copy-Item "data\auremgrid.sqlite-wal" "data\incident_snapshot.sqlite-wal" -ErrorAction SilentlyContinue
   ```

2. **Capture Running & Leased Jobs**:
   ```powershell
   .venv\Scripts\python.exe -c "import sqlite3; conn = sqlite3.connect('data/auremgrid.sqlite'); print(conn.execute('SELECT id, type, status, attempts, lease_owner, last_error FROM jobs WHERE status IN ('leased', 'running', 'failed')').fetchall())"
   ```

3. **Capture Pending Outbox Events**:
   ```powershell
   .venv\Scripts\python.exe -c "import sqlite3; conn = sqlite3.connect('data/auremgrid.sqlite'); print(conn.execute('SELECT id, event_type, status, attempts, last_error FROM outbox_events WHERE status != 'published'').fetchall())"
   ```

4. **Capture Recent Audit Events**:
   ```powershell
   .venv\Scripts\python.exe -c "import sqlite3; conn = sqlite3.connect('data/auremgrid.sqlite'); print(conn.execute('SELECT * FROM ledger_audit ORDER BY created_at DESC LIMIT 25').fetchall())"
   ```

5. **Capture Detailed Health Output**:
   ```powershell
   Invoke-RestMethod -Uri "http://127.0.0.1:8787/health/detailed" | ConvertTo-Json -Depth 5
   ```

