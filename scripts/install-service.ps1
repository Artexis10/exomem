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
#   pwsh -File scripts/install-service.ps1 -NssmPath "C:\path\to\nssm.exe"
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
    [ValidateSet("lean", "hybrid", "standard", "media")]
    [string]$Profile = "standard",
    [switch]$Release,
    [string]$ServiceRoot = "",
    [string]$PackageVersion = "",
    [ValidateSet("auto", "always", "never")]
    [string]$CudaTorch = "auto",
    [switch]$ResumeStoppedTransition,
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
    if ($ResumeStoppedTransition) { $relaunchArgs += "-ResumeStoppedTransition" }
    Start-Process -FilePath $hostExe -Verb RunAs -ArgumentList $relaunchArgs
    exit
}

. "$PSScriptRoot\_service-common.ps1"

$script:StateRootTransitionBegan = $false
$workerBefore = 0
$workerAfter = 0
$transitionReceipt = $null
$transitionIdentity = $null
$existingTransition = $false

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
    if ($existingTransition -and $transitionIdentity) {
        try {
            Publish-ExomemFailedTransitionReceipt `
                @transitionIdentity `
                -ObservedWorkerPid $workerAfter | Out-Null
        } catch {
            Write-Host "Could not update the retained transition receipt: $($_.Exception.Message)" -ForegroundColor Red
        }
    }
    & $NssmPath stop $ServiceName | Out-Null
    try {
        if (Get-Service -Name $ServiceName -ErrorAction SilentlyContinue) {
            Wait-ServiceState -Name $ServiceName -Target 'Stopped' -TimeoutSec 30
        }
        if ($existingTransition) {
            $transitionReceipt = Read-ExomemTransitionReceipt @transitionIdentity
            Assert-ExomemStoppedResumeAuthority -ServiceName $ServiceName -Receipt $transitionReceipt
        } else {
            if (Get-Service -Name $ServiceName -ErrorAction SilentlyContinue) {
                Assert-ExomemServiceStopped -ServiceName $ServiceName -CapturedPids @($workerAfter) -Ports @($Port)
            } else {
                Assert-ExomemConfiguredPortUnbound -ServiceName $ServiceName -Port $Port
            }
        }
        Write-Host "State-root transition failed; service remains stopped." -ForegroundColor Yellow
    } catch {
        Write-Host "Could not prove the failed transition stopped the service: $($_.Exception.Message)" -ForegroundColor Red
    }
}

trap {
    Stop-FailedStateRootTransition
    Write-Host "SERVICE INSTALL FAILED: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
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
    # Merge by name so an in-place upgrade preserves opaque entries that are
    # not represented in this checkout's dotenv. Never render the values.
    $merged = [ordered]@{}
    $key = "HKLM:\SYSTEM\CurrentControlSet\Services\$ServiceName\Parameters"
    try {
        $parameters = Get-ItemProperty -Path $key -ErrorAction Stop
        if ($parameters.PSObject.Properties.Name -contains "AppEnvironmentExtra") {
            foreach ($entry in @($parameters.AppEnvironmentExtra)) {
                $text = [string]$entry
                $separator = $text.IndexOf('=')
                if ($separator -gt 0) {
                    $merged[$text.Substring(0, $separator)] = $text.Substring($separator + 1)
                }
            }
        }
    } catch {
        $merged = [ordered]@{}
    }
    foreach ($entry in $Map.GetEnumerator()) {
        $merged[$entry.Key] = [string]$entry.Value
    }
    $args = @("set", $ServiceName, "AppEnvironmentExtra")
    foreach ($entry in $merged.GetEnumerator()) {
        $args += "$($entry.Key)=$($entry.Value)"
    }
    & $NssmPath @args | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to update the service environment (nssm exit $LASTEXITCODE)."
    }
}

function Test-McpEndpoint {
    param(
        [string]$HostName,
        [int]$EndpointPort,
        [int]$TimeoutSec = 60
    )
    $verifyHost = if ($HostName -in @("0.0.0.0", "::", "[::]")) { "127.0.0.1" } else { $HostName }
    $url = "http://${verifyHost}:${EndpointPort}/mcp"
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    $lastStatus = 0
    while ((Get-Date) -lt $deadline) {
        try {
            $response = Invoke-WebRequest -Uri $url -Method Get -SkipHttpErrorCheck -TimeoutSec 2
            $lastStatus = [int]$response.StatusCode
        } catch {
            $lastStatus = 0
        }
        if ($lastStatus -eq 401) {
            Write-Host "Verified $url -> 401 (healthy, OAuth enforced)."
            return
        }
        if ($lastStatus -eq 200) {
            throw "$url returned 200; OAuth is not enforced."
        }
        Start-Sleep -Seconds 1
    }
    throw "$url did not return the expected OAuth 401 (last status: $lastStatus)."
}

function Install-ReleaseVenv {
    # Prefer the venv this box is ALREADY installed against. Re-running the
    # installer to upgrade must land in the same place, and the directory name is
    # whatever the original -ServiceRoot said -- 'exomem-service-ha' on this box,
    # not the 'exomem-service-release' default. Guessing wrong silently provisions
    # a second venv and leaves the service running the old one.
    $existingRoot = Get-ExomemServiceRoot -PythonPath (Get-ExomemServicePython -ServiceName $ServiceName)
    $root = if ($ServiceRoot) {
        $ServiceRoot
    } elseif ($existingRoot) {
        Write-Host "Reusing the venv '$ServiceName' is already installed against: $existingRoot"
        $existingRoot
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

    Install-ExomemPackage -Python $venvPython -Profile $Profile -PackageVersion $PackageVersion
    Repair-TorchCuda -Python $venvPython -Profile $Profile -CudaTorch $CudaTorch
    return $venvPython
}

# --- STATE-ROOT MAIN TRANSITION: stop/prove/install/migrate/doctor/start -------
# An in-place install is an offline state transition. Capture the exact legacy
# worker before stopping; SCM state alone cannot reveal an orphan after NSSM's
# parent pid has disappeared.
$existingService = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
$vault = Get-DotenvValue "EXOMEM_VAULT_PATH"
if (-not $vault) {
    throw "EXOMEM_VAULT_PATH is required in .env for explicit offline state migration."
}
$bindingPath = Join-Path $repoRoot ".env"
$pinnedStateRoot = Resolve-ExomemDotenvStateRootBinding -AppDirectory $repoRoot
if ($existingService) {
    $existingAppDirectory = Get-ExomemServiceAppDirectory -ServiceName $ServiceName
    if (-not $existingAppDirectory -or [System.IO.Path]::GetFullPath($existingAppDirectory) -ne [System.IO.Path]::GetFullPath($repoRoot)) {
        throw "Existing service '$ServiceName' uses a different AppDirectory; its sticky state binding cannot be re-authored by this installer."
    }
    $receiptPython = Get-ExomemServicePython -ServiceName $ServiceName
    if (-not $receiptPython) {
        throw "Could not resolve the existing service interpreter for its durable transition receipt."
    }
    $oldEndpoint = Get-ExomemServiceEndpoint -ServiceName $ServiceName
    $transitionIdentity = @{
        PythonPath = $receiptPython
        ServiceName = $ServiceName
        BindingPath = $bindingPath
        StateRoot = $pinnedStateRoot
        VaultPath = $vault
        TargetPort = $Port
    }
    $existingTransition = $true
    if ($existingService.Status -eq 'Running') {
        $workerBefore = Get-ExomemServiceWorkerPid -ServiceName $ServiceName
        if (-not $workerBefore) {
            throw "Could not capture the running service worker pid; refusing an unprovable offline migration window."
        }
        $listenerPidsBefore = @(Get-ExomemConfiguredListenerPids -ServiceName $ServiceName)
        $transitionReceipt = New-ExomemTransitionReceipt @transitionIdentity `
            -Port ([int]$oldEndpoint.Port) `
            -WorkerPid $workerBefore `
            -ListenerPids $listenerPidsBefore
        $script:StateRootTransitionBegan = $true
        Write-Host "Stopping '$ServiceName' before replacing its interpreter..."
        & $NssmPath stop $ServiceName | Out-Null
        Wait-ServiceState -Name $ServiceName -Target 'Stopped'
        Assert-ExomemServiceStopped `
            -ServiceName $ServiceName `
            -CapturedPids $transitionReceipt.proof_pids `
            -Ports @($transitionReceipt.port, $transitionReceipt.target_port)
    } elseif ($ResumeStoppedTransition -and $existingService.Status -eq 'Stopped') {
        $transitionReceipt = Read-ExomemTransitionReceipt @transitionIdentity
        Assert-ExomemStoppedResumeAuthority -ServiceName $ServiceName -Receipt $transitionReceipt
        $workerBefore = [int]$transitionReceipt.worker_pid
        $script:StateRootTransitionBegan = $true
    } else {
        throw "Existing service '$ServiceName' must be running for first entry; use -ResumeStoppedTransition only to continue a previously failed, proven-stopped transition."
    }
    Set-ExomemTransitionReceiptPhase @transitionIdentity -Phase "stopped"
} else {
    Assert-ExomemConfiguredPortUnbound -ServiceName $ServiceName -Port $Port
    $script:StateRootTransitionBegan = $true
}

$receiptStateRoot = if ($transitionReceipt) { [string]$transitionReceipt.state_root } else { $pinnedStateRoot }
$pinnedStateRoot = Ensure-ExomemDotenvStateRootBinding `
    -AppDirectory $repoRoot `
    -ExpectedStateRoot $receiptStateRoot
[Environment]::SetEnvironmentVariable("EXOMEM_STATE_ROOT", $pinnedStateRoot, "Process")
if ($existingTransition) {
    Set-ExomemTransitionReceiptPhase @transitionIdentity -Phase "bound"
}

if ($Release) {
    $python = Install-ReleaseVenv
    # Wheel-backed service venv: `resolve_log_dir()` resolves an unset
    # EXOMEM_LOG_DIR to %ProgramData%\exomem\logs there (not a checkout).
    # The last tier is spelled as a concatenation, exactly as the Python side
    # (`_user_log_dir()`, `mode.config_path()`) spells its own `"C:" + r"\ProgramData"`:
    # the literal must stay byte-identical to Python's, so `$env:SystemDrive` is
    # deliberately NOT used -- it resolves to a non-C: volume on a box where Windows
    # was installed elsewhere, which would silently pin a directory the service's own
    # `resolve_log_dir()` would never pick.
    $logDirBase = if ($env:ProgramData) { $env:ProgramData } elseif ($env:ALLUSERSPROFILE) { $env:ALLUSERSPROFILE } else { "C:" + "\ProgramData" }
    $pinnedLogDir = Join-Path $logDirBase "exomem\logs"
} else {
    $python = Join-Path $repoRoot ".venv\Scripts\python.exe"
    if (-not (Test-Path $python)) {
        throw "Python venv not found at $python. Run 'uv sync' in $repoRoot first, or pass -Release to install a PyPI-backed service venv."
    }
    # Editable checkout: `resolve_log_dir()` resolves an unset EXOMEM_LOG_DIR
    # to <repo>\logs there -- the same $logDir this script already uses for
    # the NSSM stdout/stderr redirects, and the same folder `restart.ps1`
    # tails.
    $pinnedLogDir = $logDir
}

$installedVersion = Get-ExomemInstalledVersion -PythonPath $python
if (-not $installedVersion) {
    throw "The target interpreter has no importable Exomem version after installation."
}
if ($existingTransition) {
    Set-ExomemTransitionReceiptPhase @transitionIdentity -Phase "installed"
}

# Pin EXOMEM_LOG_DIR explicitly rather than let the running service
# independently re-derive its own fallback. `$pinnedLogDir` is computed per
# install mode above to be the SAME value `resolve_log_dir()` (issue #552)
# already resolves an unset EXOMEM_LOG_DIR to for the interpreter this
# install will actually run -- %ProgramData%\exomem\logs for the wheel-backed
# -Release venv, <repo>\logs for a repo-mode editable checkout -- so the pin
# changes behavior in NEITHER mode today. What it buys is that the service
# (via AppEnvironmentExtra below) and any operator-run CLI on this box
# (`exomem doctor`, `exomem trace`) PROVABLY agree, rather than merely
# happening to because both sides independently compute the identical
# formula. It also survives a future change to that fallback without silently
# moving where a running service's logs land underneath an operator who never
# re-ran this script. Respects an operator's own explicit `.env` override --
# only pins when unset there.
$serviceEnv = Read-DotenvMap
if (-not $serviceEnv.Contains("EXOMEM_LOG_DIR")) {
    $serviceEnv["EXOMEM_LOG_DIR"] = $pinnedLogDir
}
$appLogDir = $serviceEnv["EXOMEM_LOG_DIR"]
Set-ProcessEnvFromMap -Map $serviceEnv

$script:StateRootTransitionBegan = $true
Write-Host "Migrating machine-local state under the proven offline window..."
$migration = Invoke-ExomemNative -CommandArgs @(
    $python, "-m", "exomem", "maintain", "--vault", $vault,
    "--migrate-state", "--offline", "--json"
)
if ($migration.ExitCode -ne 0) {
    throw "Offline state migration failed; '$ServiceName' remains stopped."
}
if ($existingTransition) {
    Set-ExomemTransitionReceiptPhase @transitionIdentity -Phase "migrated"
}

$doctorArgs = @("-m", "exomem", "doctor", "--profile", $Profile, "--vault", $vault)
Write-Host "Preflight: exomem doctor --profile $Profile..."
& $python @doctorArgs
if ($LASTEXITCODE -ne 0) {
    throw "Doctor preflight failed for profile '$Profile'; '$ServiceName' remains stopped."
}

$remoteDoctorArgs = @("-m", "exomem", "doctor", "--profile", "remote", "--vault", $vault)
Write-Host "Preflight: exomem doctor --profile remote..."
& $python @remoteDoctorArgs
if ($LASTEXITCODE -ne 0) {
    throw "Remote doctor preflight failed; '$ServiceName' remains stopped."
}

# Install, or reconfigure in place when already registered. `nssm install` against
# an existing service fails, which made re-running this script -- the documented
# way to upgrade -- unsafe, so nobody re-ran it and the service drifted 5 releases
# behind. Every `set` below is idempotent, so the two paths converge. Registration
# stays after both doctor gates so a fresh failed preflight leaves no auto-start
# service behind and the next ordinary invocation remains a fresh install.
if ($existingService) {
    Write-Host "Service '$ServiceName' is already registered; reconfiguring in place."
    & $NssmPath set $ServiceName Application $python
    & $NssmPath set $ServiceName AppParameters "-m exomem --transport streamable-http --host $BindHost --port $Port"
} else {
    & $NssmPath install $ServiceName $python "-m" "exomem" "--transport" "streamable-http" "--host" $BindHost "--port" $Port
}
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

if ($existingTransition) {
    Set-ExomemTransitionReceiptPhase @transitionIdentity -Phase "doctor-passed"
    Set-ExomemTransitionReceiptPhase @transitionIdentity -Phase "starting"
}

& $NssmPath start $ServiceName
Wait-ServiceState -Name $ServiceName -Target 'Running'
$workerAfter = Get-ExomemServiceWorkerPid -ServiceName $ServiceName
if ($existingTransition) {
    Set-ExomemTransitionReceiptPhase @transitionIdentity -Phase "starting" -ObservedPids @($workerAfter)
}
if ($workerBefore) {
    Assert-ExomemServiceRestarted -Before $workerBefore -After $workerAfter -ServiceName $ServiceName
} elseif (-not $workerAfter) {
    throw "The new service has no observable worker process after start."
}
Assert-ExomemListenerOwnedByWorker -ServiceName $ServiceName -WorkerPid $workerAfter
$verifyHost = if ($BindHost -in @("0.0.0.0", "::", "[::]")) { "127.0.0.1" } else { $BindHost }
$healthUrl = "http://${verifyHost}:${Port}/health"
$health = Wait-ExomemHealthVersion `
    -HealthUrl $healthUrl `
    -ExpectedVersion $installedVersion `
    -TimeoutSec 90
if ($existingTransition) {
    Set-ExomemTransitionReceiptPhase @transitionIdentity -Phase "started"
}

try {
    Test-McpEndpoint -HostName $BindHost -EndpointPort $Port
} catch {
    throw "Service endpoint verification failed: $_"
}
if ($existingTransition) {
    Set-ExomemTransitionReceiptPhase @transitionIdentity -Phase "accepted"
    Remove-ExomemTransitionReceipt @transitionIdentity
}
$script:StateRootTransitionBegan = $false

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

$connectorPending = $false
$connectorContractPath = Join-Path $repoRoot "deploy\chatgpt\personal-plugin-contract.json"
if (Test-Path $connectorContractPath) {
    try {
        $connectorContract = Get-Content -Raw $connectorContractPath | ConvertFrom-Json
        $connectorPending = $connectorContract.refresh_required -eq $true
    } catch {
        Write-Warning "Could not read ChatGPT connector rollout contract: $_"
    }
}

if ($connectorPending) {
    Write-Warning "CHATGPT_PLUGIN_REFRESH_REQUIRED: service is running, but connector rollout is incomplete."
    Write-Host "  Confirm /health/ready.mcp_tool_surface_sha256 matches pending_tool_surface_sha256."
    Write-Host "  Refresh or recreate the ChatGPT Personal Plugin, then invoke bootstrap and ask_memory from a fresh conversation."
    Write-Host "  Promote the pending digest to registered_tool_surface_sha256 only after that smoke test passes."
    Write-Host "Installed and started service '$ServiceName' bound to ${BindHost}:${Port}; connector promotion remains pending."
} else {
    Write-Host "Installed, started, and connector-cleared service '$ServiceName' bound to ${BindHost}:${Port}."
}
Write-Host "Logs: $logDir\service.out.log (stdout), service.err.log (stderr, NSSM-rotated); $appLogDir\exomem.log (app, EXOMEM_LOG_DIR)"
