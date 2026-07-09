# Install exomem as a Windows service via NSSM.
#
# Prereqs:
#   - NSSM installed (https://nssm.cc/download) and on PATH, OR pass -NssmPath.
#   - uv installed and on PATH.
#   - For repo-mode installs, `uv sync` has been run in repo root so .venv exists.
#     For release installs, pass -Release and the script creates/updates a sibling
#     PyPI-backed service venv.
#   - .env exists in the repo root with the GitHub OAuth vars set
#     (EXOMEM_BASE_URL, EXOMEM_GITHUB_USERNAME, GITHUB_CLIENT_ID, GITHUB_CLIENT_SECRET).
#
# Usage:
#   pwsh -File scripts/install-service.ps1 -Release
#   pwsh -File scripts/install-service.ps1 -NssmPath "C:\nssm\nssm.exe"
#   pwsh -File scripts/install-service.ps1 -Release -Profile lean    # lexical-only service
#   pwsh -File scripts/install-service.ps1 -Release -Profile media
#
# Uninstall:
#   nssm stop exomem
#   nssm remove exomem confirm

param(
    [string]$NssmPath = "nssm",
    [string]$ServiceName = "exomem",
    [string]$BindHost = "127.0.0.1",
    [int]$Port = 8765,
    [ValidateSet("lean", "hybrid", "media")]
    [string]$Profile = "hybrid",
    [switch]$Release,
    [string]$ServiceRoot = "",
    [string]$PackageVersion = "",
    [ValidateSet("auto", "always", "never")]
    [string]$CudaTorch = "auto",
    [switch]$LegacyMcpCompat
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
        "-Port", $Port,
        "-Profile", $Profile,
        "-CudaTorch", $CudaTorch
    )
    if ($Release) { $relaunchArgs += "-Release" }
    if ($ServiceRoot) { $relaunchArgs += @("-ServiceRoot", "`"$ServiceRoot`"") }
    if ($PackageVersion) { $relaunchArgs += @("-PackageVersion", "`"$PackageVersion`"") }
    if ($LegacyMcpCompat) { $relaunchArgs += "-LegacyMcpCompat" }
    Start-Process -FilePath $hostExe -Verb RunAs -ArgumentList $relaunchArgs
    exit
}

$repoRoot = (Resolve-Path "$PSScriptRoot\..").Path
$logDir = Join-Path $repoRoot "logs"

if (-not (Test-Path (Join-Path $repoRoot ".env"))) {
    throw ".env file missing in $repoRoot. See the Install section of README.md for the required GitHub OAuth vars."
}
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }

function Get-DotenvValue {
    param([string]$Name)
    $envPath = Join-Path $repoRoot ".env"
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

function Read-DotenvMap {
    $envPath = Join-Path $repoRoot ".env"
    $map = [ordered]@{}
    foreach ($line in Get-Content $envPath) {
        if ($line -match '^\s*([^#][A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)\s*$') {
            $name = $Matches[1].Trim()
            $value = $Matches[2].Trim()
            if (($value.StartsWith('"') -and $value.EndsWith('"')) -or ($value.StartsWith("'") -and $value.EndsWith("'"))) {
                $value = $value.Substring(1, $value.Length - 2)
            }
            $map[$name] = $value
        }
    }
    if ($LegacyMcpCompat) {
        $map["EXOMEM_MCP_LEGACY_COMPAT"] = "1"
    }
    return $map
}

function Set-ProcessEnvFromMap {
    param([System.Collections.IDictionary]$Map)
    foreach ($entry in $Map.GetEnumerator()) {
        [Environment]::SetEnvironmentVariable($entry.Key, [string]$entry.Value, "Process")
    }
}

function Set-NssmEnvironment {
    param([System.Collections.IDictionary]$Map)
    $args = @("set", $ServiceName, "AppEnvironmentExtra")
    foreach ($entry in $Map.GetEnumerator()) {
        $args += "$($entry.Key)=$($entry.Value)"
    }
    & $NssmPath @args
}

function Invoke-LoggedNative {
    param([string[]]$CommandArgs)
    $out = & $CommandArgs[0] @($CommandArgs[1..($CommandArgs.Count - 1)]) 2>&1
    foreach ($line in $out) { Write-Host $line }
    return $LASTEXITCODE
}

function Install-ReleaseVenv {
    $root = if ($ServiceRoot) {
        $ServiceRoot
    } else {
        Join-Path (Split-Path -Parent $repoRoot) "exomem-service-release"
    }
    if (-not (Test-Path $root)) { New-Item -ItemType Directory -Path $root | Out-Null }
    $venvPython = Join-Path $root ".venv\Scripts\python.exe"
    if (-not (Test-Path $venvPython)) {
        Write-Host "Creating release service venv at $root\.venv..."
        $code = Invoke-LoggedNative @("uv", "venv", (Join-Path $root ".venv"), "--python", "3.13")
        if ($code -ne 0) { throw "uv venv failed" }
    }

    $pkg = if ($PackageVersion) { "exomem==$PackageVersion" } else { "exomem" }
    if ($Profile -eq "hybrid") {
        $pkg = if ($PackageVersion) { "exomem[embeddings]==$PackageVersion" } else { "exomem[embeddings]" }
    } elseif ($Profile -eq "media") {
        $pkg = if ($PackageVersion) { "exomem[embeddings,media,vision,diarization]==$PackageVersion" } else { "exomem[embeddings,media,vision,diarization]" }
    }

    Write-Host "Installing $pkg into release service venv..."
    $code = Invoke-LoggedNative @("uv", "pip", "install", "--python", $venvPython, $pkg)
    if ($code -ne 0) { throw "uv pip install failed for $pkg" }

    $shouldCuda = $false
    if ($CudaTorch -eq "always") {
        $shouldCuda = $true
    } elseif ($CudaTorch -eq "auto" -and (Get-Command nvidia-smi -ErrorAction SilentlyContinue)) {
        $shouldCuda = $true
    }
    if ($shouldCuda -and $Profile -ne "lean") {
        Write-Host "Installing CUDA 13.2 Torch for Windows NVIDIA GPU support..."
        $code = Invoke-LoggedNative @(
            "uv",
            "pip",
            "install",
            "--python",
            $venvPython,
            "--default-index",
            "https://download.pytorch.org/whl/cu132",
            "torch==2.12.0+cu132"
        )
        if ($code -ne 0) { throw "CUDA Torch install failed" }
    }
    return $venvPython
}

if ($Release) {
    $python = Install-ReleaseVenv
} else {
    $python = Join-Path $repoRoot ".venv\Scripts\python.exe"
    if (-not (Test-Path $python)) {
        throw "Python venv not found at $python. Run 'uv sync' in $repoRoot first, or pass -Release to install a PyPI-backed service venv."
    }
}

$serviceEnv = Read-DotenvMap
Set-ProcessEnvFromMap -Map $serviceEnv

$doctorArgs = @("-m", "exomem", "doctor", "--profile", $Profile)
$vault = Get-DotenvValue "EXOMEM_VAULT_PATH"
if ($vault) { $doctorArgs += @("--vault", $vault) }
Write-Host "Preflight: exomem doctor --profile $Profile..."
& $python @doctorArgs
if ($LASTEXITCODE -ne 0) {
    throw "Doctor preflight failed for profile '$Profile'. Install the missing extras (for example: uv sync --frozen --extra embeddings) before installing the service."
}

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
Set-NssmEnvironment -Map $serviceEnv

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
