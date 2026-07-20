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

$before = Get-Provenance
if ($before) {
    $accelBefore = [bool]$before.accelerated
    Write-Host "Currently deployed: $($before.version) ($($before.install_source)), torch $($before.torch)"
} else {
    # A pre-provenance build cannot self-report; fall back to metadata so the
    # accelerator comparison still has a baseline.
    $accelBefore = $false
    $torchBefore = & $servicePython -c "import importlib.metadata as m; print(m.version('torch'))" 2>$null
    if ($torchBefore -and ($torchBefore -match '\+(cu|rocm|xpu)')) { $accelBefore = $true }
    Write-Host "Currently deployed: (pre-provenance build), torch $torchBefore"
}

if ($DryRun) {
    Write-Host "DryRun: resolved target only, no changes made." -ForegroundColor Yellow
    exit 0
}

# --- 2. Upgrade that environment ---------------------------------------------
Write-Host "`nUpgrading to exomem[$Extras]==$Version ..." -ForegroundColor Cyan
& uv pip install --python $servicePython --upgrade "exomem[$Extras]==$Version"
if ($LASTEXITCODE -ne 0) { Fail "uv pip install returned $LASTEXITCODE." }

# --- 3. Accelerator regression gate ------------------------------------------
# The cu132 pin lives in the repo's [tool.uv.sources], which a PyPI-backed venv
# cannot see, so an upgrade silently resolves the default CPU wheel on Windows.
$after = Get-Provenance
$accelAfter = if ($after) { [bool]$after.accelerated } else { $false }
$torchAfter = if ($after) { $after.torch } else { "unknown" }

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
