$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$testRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("auremgrid-git-guard-" + [guid]::NewGuid())

function Assert-Fails {
    param([scriptblock]$Action, [string]$Message)
    $previousErrorAction = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    & $Action 2>$null | Out-Null
    $ErrorActionPreference = $previousErrorAction
    if ($LASTEXITCODE -eq 0) { throw $Message }
}

try {
    New-Item -ItemType Directory -Path $testRoot | Out-Null
    $originalGlobalConfig = $env:GIT_CONFIG_GLOBAL
    $env:GIT_CONFIG_GLOBAL = Join-Path $testRoot "global.gitconfig"
    Set-Content -Path $env:GIT_CONFIG_GLOBAL -Value ""
    Copy-Item -Recurse (Join-Path $repoRoot ".githooks") (Join-Path $testRoot ".githooks")
    $chmod = Get-Command chmod -ErrorAction SilentlyContinue
    if ($null -ne $chmod) {
        & $chmod.Source +x ".githooks/pre-commit" ".githooks/commit-msg" ".githooks/pre-merge-commit"
        if ($LASTEXITCODE -ne 0) { throw "Unable to make temporary Git hooks executable." }
    }
    Push-Location $testRoot
    git init -q
    git config core.hooksPath .githooks
    git config user.name "Auremgrid"
    git config user.email "auremgrid@users.noreply.github.com"

    Set-Content -Path "good.txt" -Value "ordinary content"
    git add good.txt
    git commit -qm "Guard acceptance"
    if ($LASTEXITCODE -ne 0) { throw "Expected compliant commit to succeed." }

    Set-Content -Path "trailer.txt" -Value "trailer check"
    git add trailer.txt
    Assert-Fails { git commit -qm "Guard rejection`n`nCo-authored-by: Third Party <third@example.com>" } "Attribution trailer was accepted."
    git reset -q HEAD trailer.txt

    $reserved = "co" + "dex"
    Set-Content -Path "reserved.txt" -Value $reserved
    git add reserved.txt
    Assert-Fails { git commit -qm "Reserved word rejection" } "Reserved attribution reference was accepted."
    git reset -q HEAD reserved.txt

    Set-Content -Path "identity.txt" -Value "identity check"
    git add identity.txt
    Assert-Fails { git -c user.name="Other Person" -c user.email="other@example.com" commit -qm "Wrong identity" } "Wrong identity was accepted."
    Write-Host "Git attribution guard checks passed." -ForegroundColor Green
}
finally {
    $env:GIT_CONFIG_GLOBAL = $originalGlobalConfig
    if ((Get-Location).Path -eq $testRoot) { Pop-Location }
    if (Test-Path -LiteralPath $testRoot) { Remove-Item -LiteralPath $testRoot -Recurse -Force }
}

exit 0
