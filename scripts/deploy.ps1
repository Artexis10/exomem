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

    # Accelerator regression is a hard failure by default: a silent CPU-torch
    # downgrade is expensive to discover later. CPU-only hosts opt out.
    [switch]$AllowCpuTorch,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot

. "$PSScriptRoot\_service-common.ps1"

function Fail($msg) {
    Write-Host "DEPLOY FAILED: $msg" -ForegroundColor Red
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
if ($before) {
    Write-Host "Currently deployed: $($before.version) ($($before.install_source)), torch $torchBefore"
} else {
    Write-Host "Currently deployed: (pre-provenance build), torch $torchBefore"
}

if ($DryRun) {
    Write-Host "DryRun: resolved target only, no changes made." -ForegroundColor Yellow
    exit 0
}

# --- 2. Upgrade that environment ---------------------------------------------
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

# Whatever did move, say so. Silent drift is how both incidents stayed invisible
# until a gate happened to trip on an unrelated check.
$packagesAfter = Get-PackageSnapshot -PythonPath $servicePython
Write-Host ""
Write-DependencyDrift -Before $packagesBefore -After $packagesAfter

# --- 3. Accelerator regression gate ------------------------------------------
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

# --- 4. Preflight, then restart ----------------------------------------------
Write-Host "`nRunning doctor gate (profile: $Profile) ..." -ForegroundColor Cyan
& $servicePython -m exomem doctor --profile $Profile | Out-Null
if ($LASTEXITCODE -ne 0) { Fail "doctor preflight failed for profile '$Profile'." }

Write-Host "Restarting service ..." -ForegroundColor Cyan
& pwsh -NoProfile -File (Join-Path $PSScriptRoot "restart.ps1") -ServiceName $ServiceName -Profile $Profile
if ($LASTEXITCODE -ne 0) { Fail "restart returned $LASTEXITCODE." }

# --- 5. Verify the RUNNING process, not the installer ------------------------
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
$observed = $null
$source = $null
foreach ($attempt in 1..30) {
    Start-Sleep -Seconds 2
    try {
        $resp = Invoke-RestMethod -Uri $HealthUrl -TimeoutSec 5 -ErrorAction Stop
        $observed = $resp.version
        $source = $resp.install_source
        if ($observed -eq $Version) { break }
    } catch {
        continue
    }
}

if ($observed -ne $Version) {
    Fail "requested $Version but the running server reports '$observed'. The restart may not have taken effect."
}

Write-Host ""
Write-Host "Deployed $observed (install_source: $source, torch: $torchAfter)" -ForegroundColor Green
