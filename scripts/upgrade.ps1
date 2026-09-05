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
# Requires PowerShell 7+ (pwsh). Windows PowerShell 5.1 is refused up front rather
# than allowed to fail obscurely partway: -SkipHttpErrorCheck below is 7.0+ only.
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
    [switch]$ResumeStoppedTransition,
    [switch]$SkipRestart
)

$ErrorActionPreference = "Stop"

. "$PSScriptRoot\_service-common.ps1"

Assert-ExomemPowerShell7 -ScriptName "upgrade.ps1"

if ($SkipRestart) {
    throw "-SkipRestart is unavailable during state-root migration; an upgrade must stop, migrate, verify, and restart as one transition."
}

$RepoRoot = Split-Path -Parent $PSScriptRoot
$script:StateRootTransitionBegan = $false
$workerBefore = 0
$workerAfter = 0
$resumingStoppedTransition = $false
$transitionReceipt = $null
$transitionIdentity = $null

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

function Stop-FailedStateRootTransition {
    if (-not $script:StateRootTransitionBegan) { return }
    $observedWorker = Get-ExomemServiceWorkerPid -ServiceName $ServiceName
    if ($observedWorker -and $observedWorker -ne $workerBefore) { $workerAfter = $observedWorker }
    try {
        Publish-ExomemFailedTransitionReceipt `
            @transitionIdentity `
            -ObservedWorkerPid $workerAfter | Out-Null
    } catch {
        Write-Host "Could not update the retained transition receipt: $($_.Exception.Message)" -ForegroundColor Red
    }
    sc.exe stop $ServiceName | Out-Null
    try {
        Wait-ServiceState -Name $ServiceName -Target 'Stopped' -TimeoutSec 30
        $transitionReceipt = Read-ExomemTransitionReceipt @transitionIdentity
        Assert-ExomemStoppedResumeAuthority -ServiceName $ServiceName -Receipt $transitionReceipt
        Write-Host "State-root transition failed; service remains stopped." -ForegroundColor Yellow
    } catch {
        Write-Host "Could not prove the failed transition stopped the service: $($_.Exception.Message)" -ForegroundColor Red
    }
}

trap {
    Stop-FailedStateRootTransition
    Write-Host "UPGRADE FAILED: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
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

# Resolve the vault before entering the stop window. The migration command may
# not guess a default after the old service is already unavailable.
$serviceAppDirectory = Get-ExomemServiceAppDirectory -ServiceName $ServiceName
if (-not $serviceAppDirectory) {
    throw "Could not resolve the service AppDirectory; the shared dotenv state-root authority is unavailable."
}
$resolvedVault = if ($Vault) { $Vault } else { Get-ExomemDotenvValue -RepoRoot $serviceAppDirectory -Name "EXOMEM_VAULT_PATH" }
if (-not $resolvedVault) { $resolvedVault = $env:EXOMEM_VAULT_PATH }
if (-not $resolvedVault) {
    throw "No vault resolved (-Vault, .env, or EXOMEM_VAULT_PATH); offline state migration is required before upgrade."
}
$bindingPath = Join-Path $serviceAppDirectory ".env"
$managedStateRoot = Resolve-ExomemDotenvStateRootBinding -AppDirectory $serviceAppDirectory
$endpoint = Get-ExomemServiceEndpoint -ServiceName $ServiceName
$targetPort = [int]$endpoint.Port
$transitionIdentity = @{
    PythonPath = $ServicePy
    ServiceName = $ServiceName
    BindingPath = $bindingPath
    StateRoot = $managedStateRoot
    VaultPath = $resolvedVault
    TargetPort = $targetPort
}
$doctorArgs = @("-m", "exomem", "doctor", "--profile", $Profile, "--vault", $resolvedVault)
$serviceState = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if ($serviceState -and $serviceState.Status -eq 'Running') {
    $workerBefore = Get-ExomemServiceWorkerPid -ServiceName $ServiceName
    if (-not $workerBefore) {
        throw "Could not capture the running service worker pid; refusing an unprovable offline migration window."
    }
    $listenerPidsBefore = @(Get-ExomemConfiguredListenerPids -ServiceName $ServiceName)
} elseif ($ResumeStoppedTransition -and $serviceState -and $serviceState.Status -eq 'Stopped') {
    $transitionReceipt = Read-ExomemTransitionReceipt @transitionIdentity
    Assert-ExomemStoppedResumeAuthority -ServiceName $ServiceName -Receipt $transitionReceipt
    $workerBefore = [int]$transitionReceipt.worker_pid
    $resumingStoppedTransition = $true
} else {
    throw "Service must be running with a capturable worker for first entry; use -ResumeStoppedTransition only to continue a previously failed, proven-stopped transition."
}

# --- STATE-ROOT MAIN TRANSITION: stop/prove/install/migrate/doctor/start -------
if (-not $resumingStoppedTransition) {
    $transitionReceipt = New-ExomemTransitionReceipt @transitionIdentity `
        -Port $targetPort `
        -WorkerPid $workerBefore `
        -ListenerPids $listenerPidsBefore
}
$script:StateRootTransitionBegan = $true
if ($resumingStoppedTransition) {
    Write-Host "Continuing the explicitly proven stopped transition..."
    Assert-ExomemStoppedResumeAuthority -ServiceName $ServiceName -Receipt $transitionReceipt
} else {
    Write-Host "Stopping $ServiceName and proving the worker pid is gone..."
    sc.exe stop $ServiceName | Out-Null
    Wait-ServiceState -Name $ServiceName -Target 'Stopped'
    Assert-ExomemServiceStopped `
        -ServiceName $ServiceName `
        -CapturedPids $transitionReceipt.proof_pids `
        -Ports @($transitionReceipt.port, $transitionReceipt.target_port)
}
Set-ExomemTransitionReceiptPhase @transitionIdentity -Phase "stopped"
$managedStateRoot = Ensure-ExomemDotenvStateRootBinding `
    -AppDirectory $serviceAppDirectory `
    -ExpectedStateRoot $transitionReceipt.state_root
[Environment]::SetEnvironmentVariable("EXOMEM_STATE_ROOT", $managedStateRoot, "Process")
Set-ExomemTransitionReceiptPhase @transitionIdentity -Phase "bound"

# --- Upgrade --------------------------------------------------------------------
Install-ExomemPackage -Python $ServicePy -Profile $Profile -PackageVersion $PackageVersion
Repair-TorchCuda -Python $ServicePy -Profile $Profile -CudaTorch $CudaTorch

# Reaching here means Install-ExomemPackage already ASSERTED that $after equals the
# version it resolved before installing. This line is the receipt for that check,
# not the check itself -- reading it as the check is what let #578 through.
$after = Get-ExomemInstalledVersion -PythonPath $ServicePy
Write-Host "Installed version: $before -> $after"
Set-ExomemTransitionReceiptPhase @transitionIdentity -Phase "installed"

# --- Offline migration and preflight against the target interpreter ------------
Write-Host "Offline state migration..."
$migration = Invoke-ExomemNative -CommandArgs @(
    $ServicePy, "-m", "exomem", "maintain", "--vault", $resolvedVault,
    "--migrate-state", "--offline", "--json"
)
if ($migration.ExitCode -ne 0) {
    throw "Offline state migration failed; the service remains stopped."
}
Set-ExomemTransitionReceiptPhase @transitionIdentity -Phase "migrated"
Write-Host "Preflight: exomem doctor --profile $Profile..."
& $ServicePy @doctorArgs
if ($LASTEXITCODE -ne 0) {
    throw "Doctor preflight failed for profile '$Profile'. The target is installed but the service remains stopped; fix the findings and re-run."
}
Set-ExomemTransitionReceiptPhase @transitionIdentity -Phase "doctor-passed"

# --- Restart --------------------------------------------------------------------
Write-Host "Restarting $ServiceName..."
Set-ExomemTransitionReceiptPhase @transitionIdentity -Phase "starting"
sc.exe start $ServiceName | Out-Null
Wait-ServiceState -Name $ServiceName -Target 'Running'
$workerAfter = Get-ExomemServiceWorkerPid -ServiceName $ServiceName
Set-ExomemTransitionReceiptPhase @transitionIdentity -Phase "starting" -ObservedPids @($workerAfter)
Assert-ExomemServiceRestarted -Before $workerBefore -After $workerAfter -ServiceName $ServiceName
Assert-ExomemListenerOwnedByWorker -ServiceName $ServiceName -WorkerPid $workerAfter
Write-Host "  running."

# --- Verify what is actually serving ---------------------------------------------
# The point of the whole script: assert the LIVE process reports the version we
# just installed. A restart that silently came back on the old code is the failure
# mode this is here to catch.
$healthUrl = "http://$($endpoint.Host):$($endpoint.Port)/health"
$health = Wait-ExomemHealthVersion -HealthUrl $healthUrl -ExpectedVersion $after -TimeoutSec 90
$served = [string]$health.version

Write-Host "Serving version: $served (from $healthUrl)"
if ($after -and $served -ne $after) {
    throw "Version mismatch: installed '$after' but the live service reports '$served'. Something else is bound to $($endpoint.Port), or the restart did not take."
}
if ($repoVersion -and $served -ne $repoVersion) {
    Write-Warning "Live service is on $served but this checkout is $repoVersion. Expected when the checkout is mid-release (repo ahead of PyPI) or on an older branch (repo behind); investigate if neither applies."
}
Set-ExomemTransitionReceiptPhase @transitionIdentity -Phase "started"

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
Set-ExomemTransitionReceiptPhase @transitionIdentity -Phase "accepted"
Remove-ExomemTransitionReceipt @transitionIdentity
$script:StateRootTransitionBegan = $false
