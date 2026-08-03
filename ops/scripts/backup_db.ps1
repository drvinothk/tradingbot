<#
.SYNOPSIS
    Dumps the local Postgres trading_bot DB (via the running docker-compose
    postgres container) to a timestamped file under ops/backups/.
    Addendum hardening batch - see docs/architecture/build-plan.md's
    "Database backup/restore" entry: no dump/restore path existed before this.

.EXAMPLE
    ./backup_db.ps1
    ./backup_db.ps1 -OutDir D:\external-backups
#>

param(
    [string]$OutDir = (Join-Path (Split-Path -Parent (Split-Path -Parent $PSScriptRoot)) "ops\backups")
)

$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$ComposeFile = Join-Path $RepoRoot "ops\docker\docker-compose.local.yml"

if (-not (Test-Path $OutDir)) {
    New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
}

$Timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
$OutFile = Join-Path $OutDir "trading_bot-$Timestamp.sql"

Write-Host "Dumping trading_bot DB to $OutFile ..."
docker compose -f $ComposeFile exec -T postgres pg_dump -U trading_bot trading_bot | Out-File -FilePath $OutFile -Encoding utf8
if ($LASTEXITCODE -ne 0) {
    throw "pg_dump failed with exit code $LASTEXITCODE"
}

Write-Host "Backup written: $OutFile"
