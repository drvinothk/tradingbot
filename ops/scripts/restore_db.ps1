<#
.SYNOPSIS
    Restores a backup_db.ps1 dump into the running docker-compose postgres
    container. Defaults to a throwaway DB (trading_bot_restore_drill), NOT
    the real trading_bot dev DB - restoring into trading_bot itself
    overwrites live local data and is an explicit opt-in via -TargetDb.

.EXAMPLE
    ./restore_db.ps1 -BackupFile ops\backups\trading_bot-20260803-034500.sql
    # Drops/recreates the real dev DB from a dump - only do this deliberately:
    ./restore_db.ps1 -BackupFile ops\backups\trading_bot-20260803-034500.sql -TargetDb trading_bot
#>

param(
    [Parameter(Mandatory = $true)]
    [string]$BackupFile,

    [string]$TargetDb = "trading_bot_restore_drill"
)

$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$ComposeFile = Join-Path $RepoRoot "ops\docker\docker-compose.local.yml"

if (-not (Test-Path $BackupFile)) {
    throw "Backup file not found: $BackupFile"
}

function Invoke-Compose {
    param([string[]]$ComposeArgs)
    & docker compose -f $ComposeFile @ComposeArgs
    if ($LASTEXITCODE -ne 0) {
        throw "docker compose $($ComposeArgs -join ' ') failed with exit code $LASTEXITCODE"
    }
}

$CheckSql = "SELECT 1 FROM pg_database WHERE datname = '$TargetDb'"
$Exists = docker compose -f $ComposeFile exec -T postgres psql -U trading_bot -d postgres -tAc $CheckSql

if (-not $Exists -or $Exists.Trim() -ne "1") {
    Write-Host "Creating database $TargetDb ..."
    Invoke-Compose @("exec", "-T", "postgres", "createdb", "-U", "trading_bot", $TargetDb)
} else {
    Write-Host "Database $TargetDb already exists - restoring into it as-is."
}

Write-Host "Restoring $BackupFile into $TargetDb ..."
Get-Content $BackupFile -Raw | docker compose -f $ComposeFile exec -T postgres psql -U trading_bot -d $TargetDb -v ON_ERROR_STOP=1
if ($LASTEXITCODE -ne 0) {
    throw "psql restore failed with exit code $LASTEXITCODE"
}

Write-Host "Restore complete: $TargetDb"
