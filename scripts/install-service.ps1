# Install exomem as a Windows service via NSSM.
#
# Prereqs:
#   - NSSM installed (https://nssm.cc/download) and on PATH, OR pass -NssmPath.
#   - uv has been run (`uv sync` in repo root) so .venv exists.
#   - .env exists in the repo root with the GitHub OAuth vars set
#     (EXOMEM_BASE_URL, EXOMEM_GITHUB_USERNAME, GITHUB_CLIENT_ID, GITHUB_CLIENT_SECRET).
#
# Usage:
#   pwsh -File scripts/install-service.ps1
#   pwsh -File scripts/install-service.ps1 -NssmPath "C:\nssm\nssm.exe"
#
# Uninstall:
#   nssm stop exomem
#   nssm remove exomem confirm

param(
    [string]$NssmPath = "nssm",
    [string]$ServiceName = "exomem",
    [string]$BindHost = "127.0.0.1",
    [int]$Port = 8765
)

$ErrorActionPreference = "Stop"

# Service install/config needs a full admin token. With UAC enabled, a normal admin
# shell gets a *filtered* token and the nssm/sc calls fail ("Administrator access is
# needed" / "Access is denied") -- while the script's later Write-Host lines still
# print, making a failed run look like it succeeded. Self-elevate so behaviour is
# identical whether UAC is on (filtered token) or off (full token).
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Host "Not elevated - relaunching as administrator (approve the UAC prompt)..."
    $hostExe = (Get-Process -Id $PID).Path
    if (-not $hostExe) { $hostExe = "pwsh" }
    $relaunchArgs = @(
        "-NoExit", "-NoProfile", "-ExecutionPolicy", "Bypass",
        "-File", "`"$PSCommandPath`"",
        "-NssmPath", "`"$NssmPath`"",
        "-ServiceName", $ServiceName,
        "-BindHost", $BindHost,
        "-Port", $Port
    )
    Start-Process -FilePath $hostExe -Verb RunAs -ArgumentList $relaunchArgs
    exit
}

$repoRoot = (Resolve-Path "$PSScriptRoot\..").Path
$python = Join-Path $repoRoot ".venv\Scripts\python.exe"
$logDir = Join-Path $repoRoot "logs"

if (-not (Test-Path $python)) {
    throw "Python venv not found at $python. Run 'uv sync' in $repoRoot first."
}
if (-not (Test-Path (Join-Path $repoRoot ".env"))) {
    throw ".env file missing in $repoRoot. See the Install section of README.md for the required GitHub OAuth vars."
}
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }

# Install
& $NssmPath install $ServiceName $python "-m" "exomem" "--transport" "streamable-http" "--host" $BindHost "--port" $Port
& $NssmPath set $ServiceName AppDirectory $repoRoot
& $NssmPath set $ServiceName AppStdout (Join-Path $logDir "service.out.log")
& $NssmPath set $ServiceName AppStderr (Join-Path $logDir "service.err.log")
& $NssmPath set $ServiceName AppRotateFiles 1
& $NssmPath set $ServiceName AppRotateOnline 1
& $NssmPath set $ServiceName AppRotateBytes 10485760
& $NssmPath set $ServiceName Start SERVICE_AUTO_START
& $NssmPath set $ServiceName AppRestartDelay 5000
& $NssmPath set $ServiceName AppThrottle 10000
& $NssmPath set $ServiceName Description "exomem: Obsidian Knowledge Base MCP server for mobile claude.ai"

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
        # sc.exe reports failure via exit code + stderr text, not an exception —
        # piping to Out-Null used to swallow both and this script then claimed
        # success while the SD was unchanged (observed 2026-07-04). Check the
        # exit code AND verify the ACE actually landed before claiming it.
        $sdsetOut = & sc.exe sdset $ServiceName $newAcl 2>&1
        $sdsetExit = $LASTEXITCODE  # capture BEFORE sdshow below overwrites it
        $verifyAcl = (& sc.exe sdshow $ServiceName | Where-Object { $_ -match '^D:' } | Select-Object -First 1)
        if ($sdsetExit -ne 0) {
            Write-Warning "sc.exe sdset failed (exit ${sdsetExit}): $sdsetOut"
            Write-Warning "No-UAC grant NOT applied. Grant manually from an elevated shell: sc.exe sdset $ServiceName `"$newAcl`""
        } elseif ($verifyAcl -notmatch [Regex]::Escape($sid)) {
            Write-Warning "sc.exe sdset reported success but the ACE did not appear in sdshow; no-UAC grant NOT applied."
            Write-Warning "Grant manually from an elevated shell: sc.exe sdset $ServiceName `"$newAcl`""
        } else {
            Write-Host "Granted no-UAC start/stop rights on '$ServiceName' to $env:USERDOMAIN\$env:USERNAME."
            Write-Host "  Future restarts: sc.exe stop $ServiceName; sc.exe start $ServiceName  (no elevation needed)"
        }
    }
} catch {
    Write-Warning "Failed to grant no-UAC rights on '$ServiceName': $_"
    Write-Warning "Service is still installed and running; you can grant manually later."
}

Write-Host "Installed and started service '$ServiceName' bound to ${BindHost}:${Port}."
Write-Host "Logs: $logDir\service.out.log (stdout), service.err.log (stderr), exomem.log (app)"
