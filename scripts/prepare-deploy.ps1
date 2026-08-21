$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
$srcRoot = Join-Path $repoRoot "src"
$env:PYTHONPATH = $srcRoot

Write-Host "=== Auremgrid Deployment Readiness ===" -ForegroundColor Cyan

$python = $null
$candidates = @(
    "$env:USERPROFILE\cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe",
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
if (-not (Test-Path $envFile)) {
    Write-Host "[WARN] .env not found. Copy from .env.example." -ForegroundColor Yellow
} else {
    Write-Host "[OK] .env exists" -ForegroundColor Green
}

$dbPath = if ($env:AUREMGRID_DB_PATH) { $env:AUREMGRID_DB_PATH } else { Join-Path $repoRoot "auremgrid.sqlite" }
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

Write-Host ""
Write-Host "[OK] Compilation check..." -ForegroundColor Green -NoNewline
& $python -m compileall -q (Join-Path $srcRoot "auremgrid") 2>$null
if ($LASTEXITCODE -ne 0) { Write-Error "Compilation failed"; exit 1 }
Write-Host " passed"

Write-Host ""
Write-Host "=== Readiness Checklist ===" -ForegroundColor Cyan
Write-Host "1. Reverse proxy (nginx/caddy) configured with TLS"
Write-Host "2. Backup schedule set (auremgrid backup + verify-backup)"
Write-Host "3. Firewall restricts access to 127.0.0.1 or private network"
Write-Host "4. Worker process configured for durable jobs"
Write-Host "5. Secrets in environment or vault, never in .env in repo"
Write-Host "6. Restore rehearsal completed at least once"

Write-Host "Ready. Run: auremgrid serve --db "$dbPath" --host 127.0.0.1 --port 8791" -ForegroundColor Green