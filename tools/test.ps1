$ErrorActionPreference = "Stop"

# Resolve the repository before selecting the available Python runtime.
$repoRoot = Split-Path -Parent $PSScriptRoot
$env:PYTHONPATH = Join-Path $repoRoot "src"

$candidates = @(
    "$env:USERPROFILE\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe",
    "python",
    "py"
)

foreach ($candidate in $candidates) {
    try {
        if ($candidate -eq "py") {
            & $candidate -3.12 -m unittest discover -s (Join-Path $repoRoot "tests")
        } else {
            & $candidate -m unittest discover -s (Join-Path $repoRoot "tests")
        }
        if ($LASTEXITCODE -eq 0) {
            exit 0
        }
    } catch {
    }
}

Write-Error "No working Python 3.12+ runtime found. Install Python or run inside Codex's bundled runtime."
