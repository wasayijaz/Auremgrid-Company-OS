param(
  [Parameter(Mandatory=$true)][string]$Db,
  [Parameter(Mandatory=$true)][string]$Organization,
  [string]$Workspace,
  [string]$WorkerId = "worker-$env:COMPUTERNAME",
  [double]$PollSeconds = 1.0
)
$ErrorActionPreference = "Stop"
& python -m auremgrid worker-loop --db $Db --organization $Organization --workspace $Workspace --worker-id $WorkerId --poll-seconds $PollSeconds
