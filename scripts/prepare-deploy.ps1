$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
$srcRoot = Join-Path $repoRoot "src"
$env:PYTHONPATH = $srcRoot

Write-Host "=== Auremgrid Deployment Readiness ===" -ForegroundColor Cyan

$python = $null
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
if (-not $python) {
    Write-Error "Python 3.12+ not found."
    exit 1
}
Write-Host "[OK] Python: $python ($(& $python --version 2>&1))" -ForegroundColor Green

$envFile = Join-Path $repoRoot ".env"
$envExample = Join-Path $repoRoot ".env.example"
if (-not (Test-Path $envExample)) {
    Write-Error ".env.example is missing; deployment defaults are undocumented."
    exit 1
}
if (-not (Test-Path $envFile)) {
    Write-Host "[WARN] .env not found. Copy from .env.example." -ForegroundColor Yellow
} else {
    Write-Host "[OK] .env exists" -ForegroundColor Green
}

$dbPath = if ($env:AUREMGRID_DB_PATH) { $env:AUREMGRID_DB_PATH } elseif ($env:AUREMGRID_DB) { $env:AUREMGRID_DB } else { Join-Path $repoRoot "data\auremgrid.sqlite" }
$dbDir = Split-Path $dbPath -Parent
if (-not (Test-Path $dbDir)) {
    Write-Host "[WARN] DB directory does not exist: $dbDir" -ForegroundColor Yellow
} else {
    $testFile = Join-Path $dbDir ("_write_test_" + (Get-Random) + ".tmp")
    try {
        Set-Content -Path $testFile -Value "test" -NoNewline
        Remove-Item $testFile -Force
        Write-Host "[OK] DB path is writable: $dbPath" -ForegroundColor Green
    } catch {
        Write-Error "DB path is not writable: $dbPath"
        exit 1
    }
}

$backupDir = if ($env:AUREMGRID_BACKUP_DIR) { $env:AUREMGRID_BACKUP_DIR } else { Join-Path $repoRoot "data\backups" }
$backupParent = Split-Path $backupDir -Parent
if (-not (Test-Path $backupParent)) {
    Write-Host "[WARN] Backup parent directory does not exist: $backupParent" -ForegroundColor Yellow
} else {
    Write-Host "[OK] Backup parent exists: $backupParent" -ForegroundColor Green
}

$smokeScript = Join-Path $repoRoot "scripts\private_host_smoke.py"
if (-not (Test-Path $smokeScript)) {
    Write-Error "Private-host smoke script is missing: $smokeScript"
    exit 1
}
Write-Host "[OK] Python private-host smoke rehearsal is available" -ForegroundColor Green

Write-Host ""
Write-Host "[OK] Compilation check..." -ForegroundColor Green -NoNewline
& $python -m compileall -q (Join-Path $srcRoot "auremgrid") 2>$null
if ($LASTEXITCODE -ne 0) { Write-Error "Compilation failed"; exit 1 }
Write-Host " passed"

Write-Host ""
Write-Host "=== Readiness Checklist ===" -ForegroundColor Cyan
Write-Host "1. Reverse proxy (nginx/caddy) configured with TLS"
Write-Host "2. Backup schedule not installed by this script; install and verify auremgrid backup + verify-backup manually"
Write-Host "3. Firewall restricts access to 127.0.0.1 or private network"
Write-Host "4. Worker process configured for durable jobs"
Write-Host "5. Secrets in environment or vault; .env.example contains no secret values"
Write-Host "6. Restore rehearsal completed with recovery mode and outbound dispatch disabled"

Write-Host "Ready. Run: auremgrid serve --db "$dbPath" --host 127.0.0.1 --port 8791" -ForegroundColor Green
Write-Host "Smoke rehearsal: & $python scripts\private_host_smoke.py" -ForegroundColor Green
