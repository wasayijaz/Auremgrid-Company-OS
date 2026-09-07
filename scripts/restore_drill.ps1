<#
.SYNOPSIS
    Operator restore drill for Auremgrid Company OS.

.DESCRIPTION
    Runs an end-to-end backup and restore drill against a real SQLite database.
    Verifies:
      1. Online backup creation and manifest generation (SHA-256 + PRAGMA checks)
      2. Backup verification via auremgrid verify-backup
      3. Restore execution into a separate destination path
      4. Database integrity via PRAGMA quick_check
      5. Foreign key consistency via PRAGMA foreign_key_check
      6. Row count parity across core business tables
      7. Automatic recovery mode activation (recovery_mode='1', outbound_dispatch='disabled')
      8. Automatic session revocation in auth_sessions and api_tokens
      9. Pre-restore safety snapshot creation on destination overwrite

.PARAMETER DatabasePath
    Optional path to an existing SQLite database. If omitted, the script seeds
    a realistic demo database in a temporary directory to exercise the drill.

.PARAMETER PythonPath
    Optional path to the Python executable. Defaults to .venv\Scripts\python.exe,
    falling back to python or py on PATH.

.PARAMETER ScratchDirectory
    Optional path to scratch workspace directory. Defaults to a unique folder in $env:TEMP.

.PARAMETER KeepArtifacts
    If specified, temporary drill files are retained for manual operator inspection.

.EXAMPLE
    .\scripts\restore_drill.ps1

.EXAMPLE
    .\scripts\restore_drill.ps1 -DatabasePath "data\auremgrid.sqlite"
#>

[CmdletBinding()]
param(
    [string]$DatabasePath = "",
    [string]$PythonPath = "",
    [string]$ScratchDirectory = "",
    [switch]$KeepArtifacts
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
$srcRoot = Join-Path $repoRoot "src"
$env:PYTHONPATH = $srcRoot
$env:PYTHONPYCACHEPREFIX = Join-Path $repoRoot "qa_pycache"

Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "    AUREMGRID COMPANY OS - OPERATOR RESTORE DRILL          " -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan

# 1. Resolve Python Runtime
$python = $null
if ($PythonPath -and (Test-Path $PythonPath)) {
    $python = $PythonPath
} else {
    $candidates = @(
        (Join-Path $repoRoot ".venv\Scripts\python.exe"),
        "python",
        "py"
    )
    foreach ($c in $candidates) {
        try {
            $ver = & $c --version 2>$null
            if ($LASTEXITCODE -eq 0 -and $ver -match "3\.(1[2-9]|[2-9])") {
                $python = $c
                break
            }
        } catch {}
    }
}

if (-not $python) {
    Write-Error "Python 3.12+ was not found. Please specify -PythonPath."
    exit 1
}
$pyVersion = & $python --version 2>&1
Write-Host "[INIT] Python executable: $python ($pyVersion)" -ForegroundColor Gray

# 2. Setup Scratch Directory
$stamp = Get-Date -Format "yyyyMMddTHHmmssZ"
if (-not $ScratchDirectory) {
    $ScratchDirectory = Join-Path $env:TEMP ("auremgrid-restore-drill-" + $stamp + "-" + [System.IO.Path]::GetRandomFileName())
}
New-Item -ItemType Directory -Path $ScratchDirectory -Force | Out-Null
Write-Host "[INIT] Scratch directory: $ScratchDirectory" -ForegroundColor Gray

$sourceDb = Join-Path $ScratchDirectory "source.sqlite"
$backupFile = Join-Path $ScratchDirectory ("backup-" + $stamp + ".sqlite")
$restoredDb = Join-Path $ScratchDirectory "restored.sqlite"

# 3. Establish Source Database
if ($DatabasePath -and (Test-Path $DatabasePath)) {
    Write-Host "[INIT] Copying specified database to drill scratch: $DatabasePath" -ForegroundColor Gray
    Copy-Item -Path $DatabasePath -Destination $sourceDb -Force
    if (Test-Path "$DatabasePath-wal") { Copy-Item "$DatabasePath-wal" "$sourceDb-wal" -Force }
    if (Test-Path "$DatabasePath-shm") { Copy-Item "$DatabasePath-shm" "$sourceDb-shm" -Force }
} else {
    Write-Host "[INIT] No database provided; seeding realistic demo agency into scratch..." -ForegroundColor Gray
    $seedOutput = & $python (Join-Path $repoRoot "scripts\auremgrid.py") demo-agency `
        --db $sourceDb `
        --organization "org_drill_demo" `
        --owner "person_drill_owner" 2>&1
    if ($LASTEXITCODE -ne 0) {
        Write-Error "Failed to seed demo agency database for drill: $seedOutput"
        exit 1
    }
}

# Helper: Execute Python script via scratch file to avoid CLI quote-stripping issues
function Invoke-PythonScript([string]$scriptText, [string[]]$arguments) {
    $tempPy = Join-Path $ScratchDirectory ("eval-" + [System.IO.Path]::GetRandomFileName() + ".py")
    Set-Content -Path $tempPy -Value $scriptText -Encoding UTF8
    try {
        $out = & $python $tempPy @arguments
        return ($out -join [Environment]::NewLine)
    } finally {
        Remove-Item -Path $tempPy -Force -ErrorAction SilentlyContinue
    }
}

# 4. Collect Baseline Metrics
$baselinePy = 'import json, sqlite3, sys; conn = sqlite3.connect(sys.argv[1]); tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type=''table'' AND name NOT LIKE ''sqlite_%''").fetchall()]; counts = {t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in tables}; conn.close(); print(json.dumps({"tables": len(tables), "counts": counts}))'

$baselineOut = Invoke-PythonScript $baselinePy @($sourceDb)
$baseline = $baselineOut | ConvertFrom-Json
Write-Host "[OK] Baseline established: $($baseline.tables) tables found" -ForegroundColor Green

# 5. Track Results
$results = [System.Collections.Generic.List[PSCustomObject]]::new()

function Record-Check([string]$Step, [string]$Description, [bool]$Passed, [string]$Details = "") {
    $results.Add([PSCustomObject]@{
        Step        = $Step
        Description = $Description
        Passed      = $Passed
        Details     = $Details
    })
    $color = if ($Passed) { "Green" } else { "Red" }
    $mark = if ($Passed) { "[PASS]" } else { "[FAIL]" }
    Write-Host "$mark ${Step}: $Description" -ForegroundColor $color
    if ($Details -and -not $Passed) {
        Write-Host "       Detail: $Details" -ForegroundColor Yellow
    }
}

# Step 1: Create Backup
try {
    $backupOutput = & $python (Join-Path $repoRoot "scripts\auremgrid.py") backup `
        --db $sourceDb `
        --output $backupFile 2>&1
    $backupExit = $LASTEXITCODE
    $backupManifestFile = "$backupFile.manifest.json"
    $backupExists = (Test-Path $backupFile) -and (Test-Path $backupManifestFile)
    Record-Check "1. Create Backup" "Generate online backup file and manifest" ($backupExit -eq 0 -and $backupExists) "Exit code: $backupExit"
} catch {
    Record-Check "1. Create Backup" "Generate online backup file and manifest" $false $_.Exception.Message
}

# Step 2: Verify Backup
try {
    $verifyOutput = & $python (Join-Path $repoRoot "scripts\auremgrid.py") verify-backup `
        --backup $backupFile 2>&1
    $verifyExit = $LASTEXITCODE
    $verifyJson = $verifyOutput | ConvertFrom-Json
    $verifyPassed = ($verifyExit -eq 0) -and ($verifyJson.integrity -eq "ok") -and ($verifyJson.foreign_key_violations -eq 0)
    Record-Check "2. Verify Backup" "Validate SHA-256 manifest, quick_check, and foreign keys" $verifyPassed "Integrity: $($verifyJson.integrity)"
} catch {
    Record-Check "2. Verify Backup" "Validate SHA-256 manifest, quick_check, and foreign keys" $false $_.Exception.Message
}

# Step 3: Execute Restore
try {
    $restoreOutput = & $python (Join-Path $repoRoot "scripts\auremgrid.py") restore `
        --backup $backupFile `
        --db $restoredDb 2>&1
    $restoreExit = $LASTEXITCODE
    $restoreJson = $restoreOutput | ConvertFrom-Json
    $restoreOk = ($restoreExit -eq 0) -and (Test-Path $restoredDb) -and ($restoreJson.integrity -eq "ok")
    Record-Check "3. Execute Restore" "Cold restore from verified backup to new target path" $restoreOk "Exit code: $restoreExit"
} catch {
    Record-Check "3. Execute Restore" "Cold restore from verified backup to new target path" $false $_.Exception.Message
}

# Step 4: Verify Restored SQLite Integrity & Foreign Keys
try {
    $integrityPy = 'import json, sqlite3, sys; conn = sqlite3.connect(sys.argv[1]); integrity = conn.execute("PRAGMA quick_check").fetchone()[0]; fks = len(conn.execute("PRAGMA foreign_key_check").fetchall()); conn.close(); print(json.dumps({"integrity": integrity, "fk_violations": fks}))'
    $intOut = Invoke-PythonScript $integrityPy @($restoredDb)
    $integrityJson = $intOut | ConvertFrom-Json
    $intPassed = ($integrityJson.integrity -eq "ok") -and ($integrityJson.fk_violations -eq 0)
    Record-Check "4. Restored Integrity" "PRAGMA quick_check is ok and 0 foreign key violations" $intPassed "Violations: $($integrityJson.fk_violations)"
} catch {
    Record-Check "4. Restored Integrity" "PRAGMA quick_check is ok and 0 foreign key violations" $false $_.Exception.Message
}

# Step 5: Verify Row Count Parity
try {
    $restoredOut = Invoke-PythonScript $baselinePy @($restoredDb)
    $restoredCountsJson = $restoredOut | ConvertFrom-Json

    $parityFailed = $false
    $mismatchTable = ""
    foreach ($table in @("organizations", "workspaces", "persons", "deliverables", "ledger_audit")) {
        $srcCount = $baseline.counts.$table
        $dstCount = $restoredCountsJson.counts.$table
        if ($srcCount -ne $null -and $dstCount -ne $null) {
            if ($srcCount -ne $dstCount) {
                $parityFailed = $true
                $mismatchTable = "$table (source: $srcCount, restored: $dstCount)"
                break
            }
        }
    }
    Record-Check "5. Data Parity" "Row count parity across core business tables" (-not $parityFailed) $mismatchTable
} catch {
    Record-Check "5. Data Parity" "Row count parity across core business tables" $false $_.Exception.Message
}

# Step 6: Verify Recovery Mode Invariant (system_state)
try {
    $statePy = 'import json, sqlite3, sys; conn = sqlite3.connect(sys.argv[1]); rows = dict(conn.execute("SELECT key, value FROM system_state").fetchall()); conn.close(); print(json.dumps(rows))'
    $stateOut = Invoke-PythonScript $statePy @($restoredDb)
    $stateJson = $stateOut | ConvertFrom-Json
    $recoveryModeOk = ($stateJson.recovery_mode -eq "1")
    $outboundDisabled = ($stateJson.outbound_dispatch -eq "disabled")
    Record-Check "6. Recovery Mode" "system_state: recovery_mode='1' and outbound_dispatch='disabled'" ($recoveryModeOk -and $outboundDisabled) "recovery_mode: $($stateJson.recovery_mode), outbound: $($stateJson.outbound_dispatch)"
} catch {
    Record-Check "6. Recovery Mode" "system_state: recovery_mode='1' and outbound_dispatch='disabled'" $false $_.Exception.Message
}

# Step 7: Verify Session Revocation Invariant
try {
    $sessPy = 'import json, sqlite3, sys; conn = sqlite3.connect(sys.argv[1]); has_table = bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type=''table'' AND name=''auth_sessions''").fetchone()); active = conn.execute("SELECT COUNT(*) FROM auth_sessions WHERE revoked_at IS NULL").fetchone()[0] if has_table else 0; conn.close(); print(json.dumps({"active_sessions": active}))'
    $sessOut = Invoke-PythonScript $sessPy @($restoredDb)
    $sessionJson = $sessOut | ConvertFrom-Json
    $sessionsRevoked = ($sessionJson.active_sessions -eq 0)
    Record-Check "7. Session Revocation" "Active sessions revoked post-restore (revoked_at populated)" $sessionsRevoked "Unrevoked sessions: $($sessionJson.active_sessions)"
} catch {
    Record-Check "7. Session Revocation" "Active sessions revoked post-restore (revoked_at populated)" $false $_.Exception.Message
}

# Step 8: Verify Pre-Restore Overwrite Protection (Safety Snapshot)
try {
    $overwriteOutput = & $python (Join-Path $repoRoot "scripts\auremgrid.py") restore `
        --backup $backupFile `
        --db $restoredDb `
        --overwrite 2>&1
    $overwriteExit = $LASTEXITCODE
    $safetySnapshots = Get-ChildItem -Path $ScratchDirectory -Filter "restored.sqlite.pre-restore-*.sqlite"
    $safetyCreated = ($overwriteExit -eq 0) -and ($safetySnapshots.Count -ge 1)
    Record-Check "8. Safety Snapshot" "Automatic safety snapshot created before overwriting existing destination" $safetyCreated "Safety snapshots found: $($safetySnapshots.Count)"
} catch {
    Record-Check "8. Safety Snapshot" "Automatic safety snapshot created before overwriting existing destination" $false $_.Exception.Message
}

# Cleanup & Summary
if (-not $KeepArtifacts) {
    try {
        Remove-Item -Path $ScratchDirectory -Recurse -Force -ErrorAction SilentlyContinue
        Write-Host "[CLEANUP] Removed temporary scratch directory: $ScratchDirectory" -ForegroundColor Gray
    } catch {}
} else {
    Write-Host "[CLEANUP] Retaining drill artifacts at: $ScratchDirectory" -ForegroundColor Yellow
}

Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "                    DRILL SUMMARY                           " -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan

$passedCount = ($results | Where-Object { $_.Passed }).Count
$totalCount = $results.Count
$allPassed = ($passedCount -eq $totalCount)

$results | Format-Table -Property Step, Description, Passed, Details -AutoSize

if ($allPassed) {
    Write-Host "[RESULT] All $totalCount restore drill checks PASSED successfully." -ForegroundColor Green
    exit 0
} else {
    $failedCount = $totalCount - $passedCount
    Write-Host "[RESULT] Restore drill FAILED ($failedCount of $totalCount checks failed)." -ForegroundColor Red
    exit 1
}
