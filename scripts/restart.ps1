# Restart the kb-mcp service properly: wait for STOP_PENDING to finish
# before starting. No UAC needed (sdset granted RPWPCR to your user).
#
# Usage:
#   pwsh -File scripts/restart.ps1
#   pwsh -File scripts/restart.ps1 -Force   # also kills orphan python.exe procs

param(
    [switch]$Force,
    [string]$ServiceName = "kb-mcp"
)

$ErrorActionPreference = "Stop"

function Wait-ServiceState {
    param([string]$Name, [string]$Target, [int]$TimeoutSec = 30)
    $start = Get-Date
    while ($true) {
        $svc = Get-Service -Name $Name -ErrorAction SilentlyContinue
        if ($svc -and $svc.Status -eq $Target) { return $true }
        if ((New-TimeSpan -Start $start -End (Get-Date)).TotalSeconds -ge $TimeoutSec) {
            throw "Timed out waiting for $Name to reach $Target (currently: $($svc.Status))"
        }
        Start-Sleep -Milliseconds 400
    }
}

Write-Host "Stopping $ServiceName..."
sc.exe stop $ServiceName | Out-Null
Wait-ServiceState -Name $ServiceName -Target 'Stopped'
Write-Host "  stopped."

if ($Force) {
    $orphans = Get-Process python -ErrorAction SilentlyContinue
    if ($orphans) {
        Write-Host "Killing $($orphans.Count) orphan python process(es)..."
        $orphans | Stop-Process -Force -ErrorAction SilentlyContinue
    }
}

# Truncate the app log so the post-restart tail shows only this session.
$logPath = Join-Path (Split-Path -Parent $PSScriptRoot) "logs\kb-mcp.log"
if (Test-Path $logPath) {
    Remove-Item $logPath -Force -ErrorAction SilentlyContinue
}

Write-Host "Starting $ServiceName..."
sc.exe start $ServiceName | Out-Null
Wait-ServiceState -Name $ServiceName -Target 'Running'
Write-Host "  running."

# Give the app a beat to write its startup banner.
Start-Sleep -Seconds 2

if (Test-Path $logPath) {
    Write-Host ""
    Write-Host "Log tail:"
    Get-Content $logPath -Tail 8
} else {
    Write-Warning "No log file at $logPath yet — service may still be initializing."
}
