# One-command exomem install for Windows. The macOS/Linux twin is scripts/install.sh.
#
#   irm https://raw.githubusercontent.com/Artexis10/exomem/main/scripts/install.ps1 | iex
#
# Installs uv if missing, installs the exomem package from PyPI, then runs the
# setup wizard (vault, MCP registration, skills, hooks).
#
# Run as a file to pass options through to the wizard:
#   pwsh -File scripts/install.ps1 -Vault "C:\Users\<user>\Obsidian" -Yes
#
# Deliberately NOT a service install. This gets you a working local exomem in
# Claude Code and Codex; scripts/install-service.ps1 is the always-on/remote path.

[CmdletBinding()]
param(
    [string]$Vault = "",
    [switch]$Yes,
    [switch]$Lean
)

$ErrorActionPreference = "Stop"

function Write-Step { param([string]$Message) Write-Host "==> $Message" -ForegroundColor Cyan }
function Write-Ok   { param([string]$Message) Write-Host "    $Message" -ForegroundColor Green }
function Write-Warn { param([string]$Message) Write-Host "    $Message" -ForegroundColor Yellow }

function Test-Command {
    param([string]$Name)
    return [bool](Get-Command $Name -ErrorAction SilentlyContinue)
}

function Add-LocalBinToPath {
    # uv installs shims here; a freshly-installed uv is not on PATH in THIS session
    # until we add it, which is what makes the one-liner work without a new shell.
    $localBin = Join-Path $env:USERPROFILE ".local\bin"
    if ((Test-Path $localBin) -and ($env:Path -notlike "*$localBin*")) {
        $env:Path = "$localBin;$env:Path"
    }
}

# --- 1. uv ----------------------------------------------------------------------
Add-LocalBinToPath
if (Test-Command "uv") {
    Write-Step "uv is already installed."
} else {
    Write-Step "Installing uv (the Python package manager exomem ships with)..."
    try {
        Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression
    } catch {
        Write-Host ""
        Write-Host "Could not install uv automatically: $_" -ForegroundColor Red
        Write-Host "Install it manually from https://docs.astral.sh/uv/getting-started/installation/"
        Write-Host "then re-run this script."
        exit 1
    }
    Add-LocalBinToPath
    if (-not (Test-Command "uv")) {
        Write-Host ""
        Write-Host "uv installed but is not on PATH yet." -ForegroundColor Yellow
        Write-Host "Open a NEW terminal, then run:  uv tool install exomem; exomem setup"
        exit 0
    }
    Write-Ok "uv installed."
}

# --- 2. exomem ------------------------------------------------------------------
Write-Step "Installing exomem from PyPI..."
& uv tool install exomem --upgrade
if ($LASTEXITCODE -ne 0) {
    Write-Host "uv tool install exomem failed (exit $LASTEXITCODE)." -ForegroundColor Red
    exit 1
}
Add-LocalBinToPath
Write-Ok "exomem installed."

if (-not (Test-Command "exomem")) {
    Write-Host ""
    Write-Warn "exomem is installed but not yet on PATH in this session."
    Write-Host "Open a NEW terminal window, then run:  exomem setup"
    exit 0
}

# --- 3. setup wizard ------------------------------------------------------------
# The wizard is interactive by default. Piping this script through `iex` keeps the
# console attached, so prompts still work -- unlike the curl|sh case on Unix, which
# needs an explicit /dev/tty reattach.
$setupArgs = @("setup")
if ($Vault) { $setupArgs += @("--vault", $Vault) }
if ($Yes)   { $setupArgs += "--yes" }
if ($Lean)  { $setupArgs += "--lean" }

Write-Step "Running the setup wizard..."
Write-Host ""
& exomem @setupArgs
$setupExit = $LASTEXITCODE

if ($setupExit -ne 0) {
    Write-Host ""
    Write-Warn "Setup did not complete (exit $setupExit)."
    Write-Host "Re-run it any time with:  exomem setup"
    Write-Host "Headless form:            exomem setup --yes --vault `"C:\path\to\your\vault`""
}
exit $setupExit
