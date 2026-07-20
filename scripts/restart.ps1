# Restart the exomem service properly: wait for STOP_PENDING to finish
# before starting. No UAC needed (sdset granted RPWPCR to your user).
#
# Usage:
#   pwsh -File scripts/restart.ps1
#   pwsh -File scripts/restart.ps1 -Force   # also kills orphan python.exe procs
#   pwsh -File scripts/restart.ps1 -Profile lean    # lexical-only service

param(
    [switch]$Force,
    [string]$ServiceName = "exomem",
    [ValidateSet("lean", "hybrid", "standard", "media")]
    [string]$Profile = "hybrid"
)

$ErrorActionPreference = "Stop"

. "$PSScriptRoot\_service-common.ps1"

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

$RepoRoot = Split-Path -Parent $PSScriptRoot

# --- Resolve the service and the venv it ACTUALLY runs -------------------------
# Back-compat with the kb-mcp -> exomem rename: boxes provisioned before the
# rename still run the service under the OLD name, so fall back before doing
# anything else (the venv lookup below is keyed on the resolved name). See
# docs/deployment.md "Renaming an existing kb-mcp service".
$resolved = Resolve-ExomemServiceName -ServiceName $ServiceName
if (-not $resolved) {
    throw "No exomem service is registered (looked for: $ServiceName, kb-mcp). Install one with scripts/install-service.ps1."
}
if ($resolved -ne $ServiceName) {
    Write-Warning "Service '$ServiceName' not found; falling back to legacy '$resolved'. Re-register under the new name with scripts/install-service.ps1 (see docs/deployment.md)."
}
$ServiceName = $resolved

# A -Release install points NSSM at a sibling PyPI-backed venv, not $RepoRoot\.venv.
# This script used to hardcode the repo venv, so on a release box the doctor gate
# below inspected an environment the service never loads -- passing or failing for
# reasons unrelated to the thing being restarted. Ask NSSM instead.
$ServicePy = Get-ExomemServicePython -ServiceName $ServiceName
if ($ServicePy) {
    $VenvPy = $ServicePy
    Write-Host "Service venv: $VenvPy"
} else {
    $VenvPy = Join-Path $RepoRoot ".venv\Scripts\python.exe"
    Write-Warning "Could not read the service interpreter from the registry; falling back to the repo venv ($VenvPy). The preflight may not reflect what the service runs."
}
$PyvenvCfg = Join-Path (Split-Path -Parent (Split-Path -Parent $VenvPy)) "pyvenv.cfg"

# --- Self-heal the interpreter ------------------------------------------------
# Kaspersky periodically quarantines the uv-managed python.exe as a false
# positive. That leaves the venv (and this service) with no interpreter, so the
# app can't start and NSSM parks the service in PAUSED - which surfaces as a 502
# at the Tailscale funnel. If the venv interpreter won't run, reinstall it
# before (re)starting. Add a Kaspersky exclusion for %APPDATA%\uv\python to stop
# the quarantine at the source; this just makes recovery automatic.

function Get-DotenvValue {
    param([string]$Name)
    $envPath = Join-Path $RepoRoot ".env"
    if (-not (Test-Path $envPath)) { return $null }
    foreach ($line in Get-Content $envPath) {
        if ($line -match "^\s*$([Regex]::Escape($Name))\s*=\s*(.*)\s*$") {
            $value = $Matches[1].Trim()
            if (($value.StartsWith('"') -and $value.EndsWith('"')) -or ($value.StartsWith("'") -and $value.EndsWith("'"))) {
                $value = $value.Substring(1, $value.Length - 2)
            }
            return $value
        }
    }
    return $null
}

function Invoke-DoctorGate {
    param([string]$Profile)
    $args = @("-m", "exomem", "doctor", "--profile", $Profile)
    $vault = Get-DotenvValue "EXOMEM_VAULT_PATH"
    if ($vault) { $args += @("--vault", $vault) }
    Write-Host "Preflight: exomem doctor --profile $Profile..."
    & $VenvPy @args
    if ($LASTEXITCODE -ne 0) {
        throw "Doctor preflight failed for profile '$Profile'. Install the missing extras (for example: uv sync --frozen --extra embeddings) before restarting."
    }
}

function Test-VenvInterpreter {
    if (-not (Test-Path $VenvPy)) { return $false }
    try { & $VenvPy --version 2>$null | Out-Null; return ($LASTEXITCODE -eq 0) }
    catch { return $false }
}

if (-not (Test-VenvInterpreter)) {
    Write-Warning "venv interpreter not runnable (Kaspersky quarantine?) - reinstalling..."
    $pyVer = $null
    if (Test-Path $PyvenvCfg) {
        $hit = Select-String -Path $PyvenvCfg -Pattern 'version_info\s*=\s*([0-9]+\.[0-9]+\.[0-9]+)'
        if ($hit) { $pyVer = $hit.Matches[0].Groups[1].Value }
    }
    # `uv python install` is a no-op if a partial dir exists, so force --reinstall.
    if ($pyVer) { uv python install $pyVer --reinstall } else { uv python install --reinstall }
    if (-not (Test-VenvInterpreter)) {
        throw "Interpreter still not runnable after reinstall. Check Kaspersky Quarantine and add an exclusion for $env:APPDATA\uv\python, then retry."
    }
    Write-Host "  interpreter restored."
}

Invoke-DoctorGate -Profile $Profile

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
$logPath = Join-Path (Split-Path -Parent $PSScriptRoot) "logs\exomem.log"
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
    Write-Warning "No log file at $logPath yet - service may still be initializing."
}
