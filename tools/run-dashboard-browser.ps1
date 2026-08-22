param(
    [string]$TestPath = "tests/browser",
    [switch]$InstallChromium
)

# Native Python writes import failures to stderr; keep checks non-terminating so
# the script can emit the actionable installation message below.
$ErrorActionPreference = "Continue"

$pythonCommand = $null
$pythonArgs = @()
$localPython = Join-Path $PSScriptRoot "..\.venv\Scripts\python.exe"
if (Test-Path -LiteralPath $localPython) {
    $pythonCommand = Get-Item -LiteralPath $localPython
} else {
    $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
}
if ($null -eq $pythonCommand) {
    $pythonCommand = Get-Command py -ErrorAction SilentlyContinue
    $pythonArgs = @("-3")
}
if ($null -eq $pythonCommand) {
    throw "Python is unavailable. Install Python 3.12+ before running dashboard browser verification."
}
$python = if ($pythonCommand -is [System.IO.FileInfo]) { $pythonCommand.FullName } else { $pythonCommand.Source }

& $python @pythonArgs -c "import playwright" 2>$null
if ($LASTEXITCODE -ne 0) {
    throw "Playwright is not installed. Run: pip install -e '.[browser]'"
}
& $python @pythonArgs -c "import pytest" 2>$null
if ($LASTEXITCODE -ne 0) {
    throw "pytest is not installed. Run: pip install -e '.[browser]' (or install pytest separately)."
}

if ($InstallChromium) {
    & $python @pythonArgs -m playwright install chromium
    if ($LASTEXITCODE -ne 0) { throw "Chromium installation failed." }
}

& $python @pythonArgs -c "from playwright.sync_api import sync_playwright; p=sync_playwright().start(); b=p.chromium.launch(headless=True); b.close(); p.stop()" 2>$null
if ($LASTEXITCODE -ne 0) {
    throw "Playwright Chromium is unavailable. Run: python -m playwright install chromium"
}

& $python @pythonArgs -m pytest -p no:cacheprovider -m browser $TestPath
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
