<#
.SYNOPSIS
  Set up, refresh, and launch the BP Work Server.

.DESCRIPTION
  Relocatable launcher: every path is resolved relative to this script, so the
  repo can live anywhere. Creates/uses a local .venv, installs the package,
  refreshes the database from the BP-Decomp_Workflow checkout, then serves.

.PARAMETER WorkflowRoot
  Path to the BP-Decomp_Workflow checkout. Defaults to $env:BP_WORKFLOW_ROOT,
  then to a sibling folder next to this repo (..\BP-Decomp_Workflow).

.PARAMETER Database
  SQLite database path. Defaults to $env:BP_WORK_DB, then data\bp-work.sqlite3.

.PARAMETER HostName
  Bind address. Default 127.0.0.1.

.PARAMETER Port
  Bind port. Default 8765.

.PARAMETER Reset
  Clear existing server data (claims, workers, events) before importing.

.PARAMETER NoImport
  Skip the workflow import and serve the database as-is.

.EXAMPLE
  .\launch.ps1
  .\launch.ps1 -HostName 0.0.0.0 -Port 8765
  .\launch.ps1 -WorkflowRoot ..\BP-Decomp_Workflow -Reset
#>
[CmdletBinding()]
param(
    [string]$WorkflowRoot,
    [string]$Database,
    [string]$HostName = "127.0.0.1",
    [int]$Port = 8765,
    [switch]$Reset,
    [switch]$NoImport
)

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot

# Resolve defaults relative to the repo, never hard-coded absolute paths.
if (-not $Database) {
    if ($env:BP_WORK_DB) { $Database = $env:BP_WORK_DB }
    else { $Database = Join-Path $root "data\bp-work.sqlite3" }
}
if (-not $WorkflowRoot) {
    if ($env:BP_WORKFLOW_ROOT) { $WorkflowRoot = $env:BP_WORKFLOW_ROOT }
    else { $WorkflowRoot = Join-Path (Split-Path $root -Parent) "BP-Decomp_Workflow" }
}

# Local virtual environment.

$venv = Join-Path $root ".venv"
$python = Join-Path $venv "Scripts\python.exe"
if (-not (Test-Path $python)) {
    Write-Host "Creating virtual environment in $venv" -ForegroundColor Cyan
    python -m venv $venv
    & $python -m pip install --upgrade pip --quiet
    & $python -m pip install -e "$root[dev]" --quiet
}

New-Item -ItemType Directory -Force -Path (Split-Path $Database -Parent) | Out-Null

# Refresh the database from the workflow checkout.
if (-not $NoImport) {
    if (-not (Test-Path $WorkflowRoot)) {
        throw "Workflow root not found: $WorkflowRoot (pass -WorkflowRoot or set BP_WORKFLOW_ROOT)"
    }
    Write-Host "Importing workflow from $WorkflowRoot" -ForegroundColor Cyan
    $importArgs = @("-m", "bp_work_server.cli", "--db", $Database, "import", $WorkflowRoot)
    if ($Reset) { $importArgs += "--reset" }
    & $python @importArgs
}

# Serve.
Write-Host "Serving on http://${HostName}:${Port}/  (db: $Database)" -ForegroundColor Green
& $python -m bp_work_server.cli --db $Database serve --host $HostName --port $Port
