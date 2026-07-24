<#
.SYNOPSIS
    Install/start/stop/remove the Trading Bot Engine Windows Service.
    Must be run from an elevated (Administrator) PowerShell prompt.

.EXAMPLE
    ./manage_service.ps1 install
    ./manage_service.ps1 start
    ./manage_service.ps1 stop
    ./manage_service.ps1 remove
#>

param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("install", "start", "stop", "remove", "debug")]
    [string]$Action
)

$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$BackendDir = Join-Path $RepoRoot "backend"
$VenvPython = Join-Path $BackendDir ".venv\Scripts\python.exe"
$ServiceScript = Join-Path $RepoRoot "ops\windows_service\service.py"

if (-not (Test-Path $VenvPython)) {
    throw "Backend venv not found at $VenvPython — run 'python -m venv .venv' and install deps in backend/ first."
}

& $VenvPython $ServiceScript $Action
