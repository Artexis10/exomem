# Deploy a released exomem version to the local NSSM service.
#
# The service interpreter is the source of truth, NOT the checkout you happen to
# be standing in. This script resolves it from NSSM, upgrades that environment,
# gates on doctor + accelerator capability, restarts, and refuses to report
# success until the RUNNING process serves the requested version.
#
# Usage:
#   pwsh -File scripts/deploy.ps1 -Version 0.25.5
#   pwsh -File scripts/deploy.ps1 -Version 0.25.5 -AllowCpuTorch   # CPU-only host
#   pwsh -File scripts/deploy.ps1 -Version 0.25.5 -DryRun          # resolve + report only

param(
    [Parameter(Mandatory = $true)]
    [string]$Version,

    [string]$ServiceName = "exomem",
    [string]$Profile = "hybrid",
    [string]$Extras = "embeddings,media",
    [string]$HealthUrl = "http://127.0.0.1:8765/health",
    [string]$NssmPath = "",
    [string]$Vault = "",

    # Accelerator regression is a hard failure by default: a silent CPU-torch
    # downgrade is expensive to discover later. CPU-only hosts opt out.
    [switch]$AllowCpuTorch,
    [switch]$ResumeStoppedTransition,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot

. "$PSScriptRoot\_service-common.ps1"

$script:StateRootTransitionBegan = $false
$workerBefore = 0
$workerAfter = 0
$resumingStoppedTransition = $false
$transitionReceipt = $null
$transitionIdentity = $null

function Wait-ServiceState {
    param([string]$Name, [string]$Target, [int]$TimeoutSec = 60)
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while ((Get-Date) -lt $deadline) {
        $service = Get-Service -Name $Name -ErrorAction SilentlyContinue
        if ($service -and $service.Status -eq $Target) { return }
        Start-Sleep -Milliseconds 400
    }
    throw "Timed out waiting for $Name to reach $Target."
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

function Fail($msg) {
    Stop-FailedStateRootTransition
    Write-Host "DEPLOY FAILED: $msg" -ForegroundColor Red
    exit 1
}

trap {
    Stop-FailedStateRootTransition
    Write-Host "DEPLOY FAILED: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}

# --- 1. Resolve the real target from the service manager ----------------------
# Never infer this from cwd. The AppDirectory can point at a checkout the
# service does not actually run from.
$nssm = if ($NssmPath) { $NssmPath } else { (Get-Command nssm -ErrorAction SilentlyContinue).Source }
if (-not $nssm -or -not (Test-Path $nssm)) {
    Fail "nssm not found. Put it on PATH or pass -NssmPath <path to nssm.exe>."
}

$servicePython = (& $nssm get $ServiceName Application) -replace "`0", ""
$servicePython = $servicePython.Trim()
if (-not $servicePython) { Fail "could not read Application for service '$ServiceName'." }
if (-not (Test-Path $servicePython)) {
    Fail "service interpreter does not exist: $servicePython"
}

Write-Host "Service interpreter: $servicePython" -ForegroundColor Cyan

$serviceAppDirectory = Get-ExomemServiceAppDirectory -ServiceName $ServiceName
if (-not $serviceAppDirectory) {
    Fail "could not resolve the service AppDirectory; the shared dotenv state-root authority is unavailable."
}
$resolvedVault = if ($Vault) { $Vault } else { Get-ExomemDotenvValue -RepoRoot $serviceAppDirectory -Name "EXOMEM_VAULT_PATH" }
if (-not $resolvedVault) { $resolvedVault = $env:EXOMEM_VAULT_PATH }
if (-not $resolvedVault) {
    Fail "no vault resolved (-Vault, .env, or EXOMEM_VAULT_PATH); offline state migration needs an explicit vault."
}
$bindingPath = Join-Path $serviceAppDirectory ".env"
$managedStateRoot = Resolve-ExomemDotenvStateRootBinding -AppDirectory $serviceAppDirectory
$endpoint = Get-ExomemServiceEndpoint -ServiceName $ServiceName
$targetPort = [int]$endpoint.Port
$transitionIdentity = @{
    PythonPath = $servicePython
    ServiceName = $ServiceName
    BindingPath = $bindingPath
    StateRoot = $managedStateRoot
    VaultPath = $resolvedVault
    TargetPort = $targetPort
}

function Get-Provenance {
    $raw = & $servicePython -m exomem install-info --json 2>$null
    if ($LASTEXITCODE -ne 0 -or -not $raw) { return $null }
    try { return $raw | ConvertFrom-Json } catch { return $null }
}

# Name -> version for everything installed, so a deploy can SAY what it changed.
# The torch gate below proves the general case matters: it exists because an
# upgrade silently swapped an accelerated build for a CPU one, and torch is not
# the only dependency that can move without anyone asking.
function Get-PackageSnapshot {
    param([string]$PythonPath)
    $raw = & uv pip list --python $PythonPath --format json 2>$null
    if ($LASTEXITCODE -ne 0 -or -not $raw) { return $null }
    $map = @{}
    try {
        foreach ($pkg in ($raw | ConvertFrom-Json)) { $map[$pkg.name] = $pkg.version }
    } catch { return $null }
    return $map
}

# Report every version that moved, with `exomem` itself excluded: it is the
# thing being deployed, so listing it as drift is noise.
function Write-DependencyDrift {
    param($Before, $After)
    if (-not $Before -or -not $After) {
        Write-Host "Dependency drift: unavailable (could not snapshot both sides)." -ForegroundColor Yellow
        return
    }
    $moved = @()
    foreach ($name in $After.Keys) {
        if ($name -ieq "exomem") { continue }
        $old = $Before[$name]
        if ($null -eq $old) { $moved += "  + $name $($After[$name]) (new)" }
        elseif ($old -ne $After[$name]) { $moved += "  ~ $name $old -> $($After[$name])" }
    }
    foreach ($name in $Before.Keys) {
        if ($name -ieq "exomem") { continue }
        if ($null -eq $After[$name]) { $moved += "  - $name $($Before[$name]) (removed)" }
    }
    if ($moved.Count -eq 0) {
        Write-Host "Dependency drift: none (only exomem changed)." -ForegroundColor Green
        return
    }
    Write-Host "Dependency drift ($($moved.Count) package(s) moved):" -ForegroundColor Yellow
    $moved | Sort-Object | ForEach-Object { Write-Host $_ -ForegroundColor Yellow }
}

# The accelerator baseline comes from torch's own distribution metadata, not
# from `install-info --json`. That payload has never carried `accelerated` or
# `torch`: the property read $null, `[bool]$null` is $false, and so the gate at
# step 3 -- documented as "a hard failure by default" -- could never fire. The
# CUDA build surviving previous deploys was luck, not the guard working.
$torchBefore = Get-ExomemTorchVersion -PythonPath $servicePython
$accelBefore = Test-ExomemAcceleratedTorch -PythonPath $servicePython

$before = Get-Provenance
$serviceState = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if ($serviceState -and $serviceState.Status -eq 'Running') {
    $workerBefore = Get-ExomemServiceWorkerPid -ServiceName $ServiceName
    if (-not $workerBefore) {
        Fail "could not capture the running service worker pid; refusing an unprovable offline migration window."
    }
    $listenerPidsBefore = @(Get-ExomemConfiguredListenerPids -ServiceName $ServiceName)
} elseif ($ResumeStoppedTransition -and $serviceState -and $serviceState.Status -eq 'Stopped') {
    $transitionReceipt = Read-ExomemTransitionReceipt @transitionIdentity
    Assert-ExomemStoppedResumeAuthority -ServiceName $ServiceName -Receipt $transitionReceipt
    $workerBefore = [int]$transitionReceipt.worker_pid
    $resumingStoppedTransition = $true
} else {
    Fail "service must be running with a capturable worker for first entry; use -ResumeStoppedTransition only to continue a previously failed, proven-stopped transition."
}
if ($before) {
    Write-Host "Currently deployed: $($before.version) ($($before.install_source)), torch $torchBefore"
} else {
    Write-Host "Currently deployed: (pre-provenance build), torch $torchBefore"
}

if ($DryRun) {
    Write-Host "DryRun: resolved target only, no changes made." -ForegroundColor Yellow
    exit 0
}

# --- 2. STATE-ROOT MAIN TRANSITION: stop/prove/install/migrate/doctor/start ---
if (-not $resumingStoppedTransition) {
    $transitionReceipt = New-ExomemTransitionReceipt @transitionIdentity `
        -Port $targetPort `
        -WorkerPid $workerBefore `
        -ListenerPids $listenerPidsBefore
}
$script:StateRootTransitionBegan = $true
if ($resumingStoppedTransition) {
    Write-Host "`nContinuing the explicitly proven stopped transition ..." -ForegroundColor Cyan
    Assert-ExomemStoppedResumeAuthority -ServiceName $ServiceName -Receipt $transitionReceipt
} else {
    Write-Host "`nStopping $ServiceName and proving the worker is gone ..." -ForegroundColor Cyan
    sc.exe stop $ServiceName | Out-Null
    if ($LASTEXITCODE -ne 0) { Fail "sc.exe stop returned $LASTEXITCODE." }
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

# --- 3. Upgrade that environment while the service remains stopped ------------
# Deliberately NOT `--upgrade`. `==$Version` pins exomem; `--upgrade` ADDITIONALLY
# floats every transitive to its newest compatible release, which is not what a
# version-pinned deploy is asking for. `sentence-transformers` is declared
# `>=2.7` with no upper bound, so that flag walked it 5.7.0 -> 6.0.0 (a major
# bump) on two separate deploys of unrelated patch releases, while the lockfile
# CI tests against pins 5.5.1 -- meaning the combination running in production
# was one nothing had ever validated.
#
# Without the flag uv still upgrades a dependency when the new exomem's own
# constraints REQUIRE it, so a genuine floor bump is honoured; it just stops
# rewriting the ML stack as a side effect. A patch release changes exomem.
#
# Deploying the lockfile instead would be worse, not better: it pins torch
# 2.12.0, and this host runs an accelerated build the lock cannot express (see
# the cu132 note in the gate below). Lock-exact would downgrade the GPU away.
$packagesBefore = Get-PackageSnapshot -PythonPath $servicePython
Write-Host "`nUpgrading to exomem[$Extras]==$Version ..." -ForegroundColor Cyan
& uv pip install --python $servicePython "exomem[$Extras]==$Version"
if ($LASTEXITCODE -ne 0) { Fail "uv pip install returned $LASTEXITCODE." }
Set-ExomemTransitionReceiptPhase @transitionIdentity -Phase "installed"

# Whatever did move, say so. Silent drift is how both incidents stayed invisible
# until a gate happened to trip on an unrelated check.
$packagesAfter = Get-PackageSnapshot -PythonPath $servicePython
Write-Host ""
Write-DependencyDrift -Before $packagesBefore -After $packagesAfter

# --- 4. Accelerator regression gate ------------------------------------------
# The cu132 pin lives in the repo's [tool.uv.sources], which a PyPI-backed venv
# cannot see, so an upgrade silently resolves the default CPU wheel on Windows.
$after = Get-Provenance
$torchAfter = Get-ExomemTorchVersion -PythonPath $servicePython
if (-not $torchAfter) { $torchAfter = "unknown" }
$accelAfter = Test-ExomemAcceleratedTorch -PythonPath $servicePython

if ($accelBefore -and -not $accelAfter) {
    Write-Host ""
    Write-Host "Accelerated torch was replaced by a CPU build ($torchAfter)." -ForegroundColor Red
    Write-Host "The CUDA pin lives in the repo's [tool.uv.sources] and does not travel"
    Write-Host "with the PyPI wheel. Restore it with:"
    Write-Host ""
    Write-Host "  uv pip install --python `"$servicePython`" --index-url https://download.pytorch.org/whl/cu132 --upgrade torch" -ForegroundColor Yellow
    Write-Host ""
    if (-not $AllowCpuTorch) {
        Fail "accelerator capability regression (pass -AllowCpuTorch to accept a CPU-only host)."
    }
    Write-Host "Continuing: -AllowCpuTorch was passed." -ForegroundColor Yellow
}

# --- 5. Offline migrate, doctor, then start -----------------------------------
Write-Host "`nMigrating machine-local state under the proven stop window ..." -ForegroundColor Cyan
$migration = Invoke-ExomemNative -CommandArgs @(
    $servicePython, "-m", "exomem", "maintain", "--vault", $resolvedVault,
    "--migrate-state", "--offline", "--json"
)
if ($migration.ExitCode -ne 0) { Fail "offline state migration failed (exit $($migration.ExitCode))." }
Set-ExomemTransitionReceiptPhase @transitionIdentity -Phase "migrated"

Write-Host "Running doctor gate (profile: $Profile) ..." -ForegroundColor Cyan
$doctor = Invoke-ExomemNative -CommandArgs @(
    $servicePython, "-m", "exomem", "doctor", "--profile", $Profile,
    "--vault", $resolvedVault
)
if ($doctor.ExitCode -ne 0) { Fail "doctor preflight failed for profile '$Profile'." }
Set-ExomemTransitionReceiptPhase @transitionIdentity -Phase "doctor-passed"

Write-Host "Starting service ..." -ForegroundColor Cyan
Set-ExomemTransitionReceiptPhase @transitionIdentity -Phase "starting"
sc.exe start $ServiceName | Out-Null
if ($LASTEXITCODE -ne 0) { Fail "sc.exe start returned $LASTEXITCODE." }
Wait-ServiceState -Name $ServiceName -Target 'Running'
$workerAfter = Get-ExomemServiceWorkerPid -ServiceName $ServiceName
Set-ExomemTransitionReceiptPhase @transitionIdentity -Phase "starting" -ObservedPids @($workerAfter)
Assert-ExomemServiceRestarted -Before $workerBefore -After $workerAfter -ServiceName $ServiceName
Assert-ExomemListenerOwnedByWorker -ServiceName $ServiceName -WorkerPid $workerAfter

# --- 6. Verify the RUNNING process and version -------------------------------
# An installer that succeeded only proves the venv changed. The deploy is not
# done until the live process serves the requested version.
#
# This check is necessary but NOT sufficient, and used to be treated as both.
# /health reports `importlib.metadata.version("exomem")`, read from disk per
# request, so it starts answering with the new version as soon as the wheel is
# replaced -- whether or not anything restarted. It caught a wrong version; it
# could not catch a stale process. The proof that the interpreter reloaded is
# the worker-pid change asserted inside restart.ps1, which step 4 just ran.
Write-Host "`nVerifying deployed version at $HealthUrl ..." -ForegroundColor Cyan
$resp = Wait-ExomemHealthVersion -HealthUrl $HealthUrl -ExpectedVersion $Version -TimeoutSec 60
$observed = [string]$resp.version
$source = [string]$resp.install_source
Set-ExomemTransitionReceiptPhase @transitionIdentity -Phase "started"
Set-ExomemTransitionReceiptPhase @transitionIdentity -Phase "accepted"
Remove-ExomemTransitionReceipt @transitionIdentity

Write-Host ""
Write-Host "Deployed $observed (install_source: $source, torch: $torchAfter)" -ForegroundColor Green
$script:StateRootTransitionBegan = $false
