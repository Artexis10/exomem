# Upgrade the installed exomem service to the current release, in one command.
#
# This exists because the service runs a PyPI-backed venv that is NOT the repo
# checkout, so `git pull` does nothing to it. Left to a manual `uv pip install`,
# two things go wrong silently: nobody remembers to run it (the service was found
# five releases behind), and a plain upgrade replaces the CUDA torch build with a
# CPU wheel because `uv pip` ignores [tool.uv.sources].
#
# No elevation required: it only writes inside the service venv and uses sc.exe
# stop/start, which your user already has rights for (install-service.ps1 grants
# RPWPCR). Re-registering the service still needs install-service.ps1.
#
# Usage:
#   pwsh -File scripts/upgrade.ps1
#   pwsh -File scripts/upgrade.ps1 -Profile media
#   pwsh -File scripts/upgrade.ps1 -PackageVersion 0.25.3   # pin instead of latest
#   pwsh -File scripts/upgrade.ps1 -SkipRestart             # stage it, restart later

param(
    [string]$ServiceName = "exomem",
    [ValidateSet("lean", "hybrid", "standard", "media")]
    [string]$Profile = "standard",
    [string]$PackageVersion = "",
    [ValidateSet("auto", "always", "never")]
    [string]$CudaTorch = "auto",
    [ValidateSet("auto", "always", "never")]
    [string]$CliSync = "auto",
    [string]$Vault = "",
    [switch]$SkipRestart
)

$ErrorActionPreference = "Stop"

. "$PSScriptRoot\_service-common.ps1"

$RepoRoot = Split-Path -Parent $PSScriptRoot

function Wait-ServiceState {
    param([string]$Name, [string]$Target, [int]$TimeoutSec = 60)
    $start = Get-Date
    while ($true) {
        $svc = Get-Service -Name $Name -ErrorAction SilentlyContinue
        if ($svc -and $svc.Status -eq $Target) { return }
        if ((New-TimeSpan -Start $start -End (Get-Date)).TotalSeconds -ge $TimeoutSec) {
            throw "Timed out waiting for $Name to reach $Target."
        }
        Start-Sleep -Milliseconds 400
    }
}

# --- Locate ---------------------------------------------------------------------
$resolved = Resolve-ExomemServiceName -ServiceName $ServiceName
if (-not $resolved) {
    throw "No exomem service is registered (looked for: $ServiceName, kb-mcp). Install one first: pwsh -File scripts/install-service.ps1 -Release"
}
$ServiceName = $resolved

$ServicePy = Get-ExomemServicePython -ServiceName $ServiceName
if (-not $ServicePy) {
    throw "Could not read the interpreter for service '$ServiceName' from the registry. If it wasn't installed by NSSM, upgrade it with scripts/install-service.ps1 instead."
}

$before = Get-ExomemInstalledVersion -PythonPath $ServicePy
$repoVersion = Get-ExomemRepoVersion -RepoRoot $RepoRoot
Write-Host "Service '$ServiceName'"
Write-Host "  venv:      $ServicePy"
Write-Host "  installed: $before"
Write-Host "  repo:      $repoVersion"

# --- Upgrade --------------------------------------------------------------------
Install-ExomemPackage -Python $ServicePy -Profile $Profile -PackageVersion $PackageVersion
Repair-TorchCuda -Python $ServicePy -Profile $Profile -CudaTorch $CudaTorch

$after = Get-ExomemInstalledVersion -PythonPath $ServicePy
Write-Host "Installed version: $before -> $after"

# --- Preflight against the venv the service actually runs ------------------------
# Vault resolution: explicit flag, then the repo .env, then the ambient env var.
# The .env only exists in the primary checkout, so a run from a git worktree needs
# one of the other two.
$doctorArgs = @("-m", "exomem", "doctor", "--profile", $Profile)
$vault = if ($Vault) { $Vault } else { Get-ExomemDotenvValue -RepoRoot $RepoRoot -Name "EXOMEM_VAULT_PATH" }
if (-not $vault) { $vault = $env:EXOMEM_VAULT_PATH }
if ($vault) { $doctorArgs += @("--vault", $vault) } else {
    Write-Warning "No vault resolved (-Vault, .env, or EXOMEM_VAULT_PATH); doctor will use its own default."
}
Write-Host "Preflight: exomem doctor --profile $Profile..."
& $ServicePy @doctorArgs
if ($LASTEXITCODE -ne 0) {
    throw "Doctor preflight failed for profile '$Profile'. The upgrade is staged in the venv but the service was NOT restarted; fix the findings and re-run."
}

if ($SkipRestart) {
    Write-Host "-SkipRestart given: the new version is staged, but the running service and user-facing CLI are unchanged. CLI sync is deferred until the live release is verified."
    exit 0
}

# --- Restart --------------------------------------------------------------------
Write-Host "Restarting $ServiceName..."
sc.exe stop $ServiceName | Out-Null
Wait-ServiceState -Name $ServiceName -Target 'Stopped'
sc.exe start $ServiceName | Out-Null
Wait-ServiceState -Name $ServiceName -Target 'Running'
Write-Host "  running."

# --- Verify what is actually serving ---------------------------------------------
# The point of the whole script: assert the LIVE process reports the version we
# just installed. A restart that silently came back on the old code is the failure
# mode this is here to catch.
$endpoint = Get-ExomemServiceEndpoint -ServiceName $ServiceName
$healthUrl = "http://$($endpoint.Host):$($endpoint.Port)/health"
$deadline = (Get-Date).AddSeconds(90)
$served = $null
while ((Get-Date) -lt $deadline) {
    try {
        $response = Invoke-WebRequest -Uri $healthUrl -TimeoutSec 5 -SkipHttpErrorCheck
        if ([int]$response.StatusCode -eq 200) {
            $served = ($response.Content | ConvertFrom-Json).version
            break
        }
    } catch {
        # Startup loads embedding/media models before binding; keep waiting.
    }
    Start-Sleep -Seconds 2
}

if (-not $served) {
    throw "Service restarted but $healthUrl never returned 200. Check logs\service.err.log."
}

Write-Host "Serving version: $served (from $healthUrl)"
if ($after -and $served -ne $after) {
    throw "Version mismatch: installed '$after' but the live service reports '$served'. Something else is bound to $($endpoint.Port), or the restart did not take."
}
if ($repoVersion -and $served -ne $repoVersion) {
    Write-Warning "Live service is on $served but this checkout is $repoVersion. Expected when the checkout is mid-release (repo ahead of PyPI) or on an older branch (repo behind); investigate if neither applies."
}

# The live process is the release authority.  Only now may a separately managed
# lean uv-tool command be aligned; -SkipRestart deliberately exits before this
# point so it can never move the CLI ahead of the running service.
$serviceTarget = "http://$($endpoint.Host):$($endpoint.Port)"
Write-ExomemManagedManifest -ServiceVersion $served -ServiceProfile $Profile -ServiceTarget $serviceTarget
$cliSynced = Sync-ExomemUvCli -Mode $CliSync -ServiceVersion $served
if ($CliSync -ne "never") {
    Assert-ExomemVisibleCliVersions -ExpectedVersion $served -RequireOne ([bool]$cliSynced -or $CliSync -eq "always")
}

$readyUrl = "http://$($endpoint.Host):$($endpoint.Port)/health/ready"
try {
    $ready = Invoke-WebRequest -Uri $readyUrl -TimeoutSec 10 -SkipHttpErrorCheck
    Write-Host "Readiness ($([int]$ready.StatusCode)): $($ready.Content)"
} catch {
    Write-Warning "Could not read $readyUrl : $_"
}
