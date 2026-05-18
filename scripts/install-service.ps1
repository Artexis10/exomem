# Install kb-mcp as a Windows service via NSSM.
#
# Prereqs:
#   - NSSM installed (https://nssm.cc/download) and on PATH, OR pass -NssmPath.
#   - uv has been run (`uv sync` in repo root) so .venv exists.
#   - .env exists in the repo root with KB_MCP_BEARER_TOKEN set.
#
# Usage:
#   pwsh -File scripts/install-service.ps1
#   pwsh -File scripts/install-service.ps1 -NssmPath "C:\nssm\nssm.exe"
#
# Uninstall:
#   nssm stop kb-mcp
#   nssm remove kb-mcp confirm

param(
    [string]$NssmPath = "nssm",
    [string]$ServiceName = "kb-mcp",
    [string]$BindHost = "127.0.0.1",
    [int]$Port = 8765
)

$ErrorActionPreference = "Stop"

$repoRoot = (Resolve-Path "$PSScriptRoot\..").Path
$python = Join-Path $repoRoot ".venv\Scripts\python.exe"
$logDir = Join-Path $repoRoot "logs"

if (-not (Test-Path $python)) {
    throw "Python venv not found at $python. Run 'uv sync' in $repoRoot first."
}
if (-not (Test-Path (Join-Path $repoRoot ".env"))) {
    throw ".env file missing in $repoRoot. Generate a token with scripts/generate-token.ps1 first."
}
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }

# Install
& $NssmPath install $ServiceName $python "-m" "kb_mcp" "--transport" "streamable-http" "--host" $BindHost "--port" $Port
& $NssmPath set $ServiceName AppDirectory $repoRoot
& $NssmPath set $ServiceName AppStdout (Join-Path $logDir "service.out.log")
& $NssmPath set $ServiceName AppStderr (Join-Path $logDir "service.err.log")
& $NssmPath set $ServiceName AppRotateFiles 1
& $NssmPath set $ServiceName AppRotateOnline 1
& $NssmPath set $ServiceName AppRotateBytes 10485760
& $NssmPath set $ServiceName Start SERVICE_AUTO_START
& $NssmPath set $ServiceName AppRestartDelay 5000
& $NssmPath set $ServiceName AppThrottle 10000
& $NssmPath set $ServiceName Description "kb-mcp: Obsidian Knowledge Base MCP server for mobile claude.ai"

& $NssmPath start $ServiceName

# Grant the invoking user start/stop rights on this service so future restarts
# don't require UAC. The ACL keeps SYSTEM/Admins/AuthenticatedUsers as-is and
# appends (A;;RPWPCR;;;<your-SID>) — RP=start, WP=stop, CR=user-defined control.
try {
    $sid = (New-Object System.Security.Principal.NTAccount("$env:USERDOMAIN\$env:USERNAME")).Translate([System.Security.Principal.SecurityIdentifier]).Value
    $currentAcl = (& sc.exe sdshow $ServiceName | Where-Object { $_ -match '^D:' } | Select-Object -First 1).Trim()
    if (-not $currentAcl) {
        Write-Warning "Could not read current service ACL via sc.exe sdshow; skipping no-UAC grant."
    } elseif ($currentAcl -match [Regex]::Escape($sid)) {
        Write-Host "User SID already in service ACL; skipping no-UAC grant."
    } else {
        $newAcl = $currentAcl + "(A;;RPWPCR;;;$sid)"
        & sc.exe sdset $ServiceName $newAcl | Out-Null
        Write-Host "Granted no-UAC start/stop rights on '$ServiceName' to $env:USERDOMAIN\$env:USERNAME."
        Write-Host "  Future restarts: sc.exe stop $ServiceName; sc.exe start $ServiceName  (no elevation needed)"
    }
} catch {
    Write-Warning "Failed to grant no-UAC rights on '$ServiceName': $_"
    Write-Warning "Service is still installed and running; you can grant manually later."
}

Write-Host "Installed and started service '$ServiceName' bound to ${BindHost}:${Port}."
Write-Host "Logs: $logDir\service.out.log (stdout), service.err.log (stderr), kb-mcp.log (app)"
