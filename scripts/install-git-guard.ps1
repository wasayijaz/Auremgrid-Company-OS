$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

git config --local user.name "Auremgrid"
if ($LASTEXITCODE -ne 0) { throw "Unable to set the repository author identity." }
git config --local user.email "auremgrid@users.noreply.github.com"
if ($LASTEXITCODE -ne 0) { throw "Unable to set the repository author email." }
git config --local core.hooksPath ".githooks"
if ($LASTEXITCODE -ne 0) { throw "Unable to activate the repository hooks." }

Write-Host "Git attribution guard is active for this clone." -ForegroundColor Green
