# Shared service-location helpers. Dot-source from the other scripts:
#   . "$PSScriptRoot\_service-common.ps1"
#
# Why this exists: the interpreter the service actually runs is NOT derivable from
# the repo layout. A release install points NSSM at a sibling PyPI-backed venv
# (scripts/install-service.ps1 -Release), whose directory name is whatever
# -ServiceRoot said at install time. Scripts that assumed "$repoRoot\.venv" have
# silently gated the wrong environment — restart.ps1 ran its doctor preflight
# against a venv the service never loads.
#
# The NSSM registry key is the single source of truth, so ask it.

# NOTE: deliberately no Set-StrictMode here. This file is dot-sourced, so any
# strictness set would leak into the caller's scope and change the behaviour of
# scripts that never opted in (restart.ps1 reads $svc.Status on a possibly-null
# service, which is fine unstrict and a hard error under StrictMode 3.0+).

# Service names to try, in order, when the caller doesn't pin one. 'kb-mcp' is the
# pre-rename name still registered on boxes provisioned before the exomem rename;
# see docs/deployment.md "Renaming an existing kb-mcp service".
$script:ExomemServiceNames = @("exomem", "kb-mcp")

function Resolve-ExomemServiceName {
    <#
    .SYNOPSIS
      Return the first installed service name, or $null when none is registered.
    #>
    param([string]$ServiceName = "")

    $candidates = if ($ServiceName) { @($ServiceName) } else { $script:ExomemServiceNames }
    foreach ($name in $candidates) {
        if (Get-Service -Name $name -ErrorAction SilentlyContinue) { return $name }
    }
    return $null
}

function Get-ExomemServicePython {
    <#
    .SYNOPSIS
      Return the interpreter path NSSM launches for $ServiceName, or $null.
    .DESCRIPTION
      Reads HKLM\SYSTEM\CurrentControlSet\Services\<name>\Parameters\Application.
      That value is REG_EXPAND_SZ, so it may carry unexpanded %VARS%. Reading it
      needs no elevation. Returns $null (never throws) when the service isn't
      installed, wasn't installed by NSSM, or the recorded path is gone — callers
      decide whether that's fatal.
    #>
    param([string]$ServiceName)

    if (-not $ServiceName) { return $null }
    $key = "HKLM:\SYSTEM\CurrentControlSet\Services\$ServiceName\Parameters"
    try {
        $params = Get-ItemProperty -Path $key -ErrorAction Stop
    } catch {
        return $null
    }
    if (-not ($params.PSObject.Properties.Name -contains "Application")) { return $null }

    $application = [Environment]::ExpandEnvironmentVariables([string]$params.Application)
    if (-not $application) { return $null }
    if (-not (Test-Path $application)) {
        Write-Warning "Service '$ServiceName' is registered against '$application', which does not exist."
        return $null
    }
    return $application
}

function Get-ExomemServiceAppDirectory {
    <# Return NSSM's dotenv/application directory without reading its contents. #>
    param([string]$ServiceName)

    if (-not $ServiceName) { return $null }
    $key = "HKLM:\SYSTEM\CurrentControlSet\Services\$ServiceName\Parameters"
    try {
        $parameters = Get-ItemProperty -Path $key -ErrorAction Stop
    } catch {
        return $null
    }
    if (-not ($parameters.PSObject.Properties.Name -contains "AppDirectory")) {
        return $null
    }
    $directory = [Environment]::ExpandEnvironmentVariables([string]$parameters.AppDirectory)
    if (-not $directory -or -not (Test-Path -LiteralPath $directory -PathType Container)) {
        return $null
    }
    return $directory
}

function Get-ExomemDefaultManagedStateRoot {
    <# Pin the installer/operator user's platform default for LocalSystem too. #>
    $base = if ($env:LOCALAPPDATA) {
        $env:LOCALAPPDATA
    } else {
        Join-Path $env:USERPROFILE "AppData\Local"
    }
    return Join-Path $base "exomem\state"
}

function Get-ExomemDotenvStateRootBinding {
    <# Read and validate the existing managed binding without changing it. #>
    param([string]$AppDirectory)

    $envPath = Join-Path $AppDirectory ".env"
    if (-not (Test-Path -LiteralPath $envPath -PathType Leaf)) {
        throw "The managed service dotenv is missing; cannot bind EXOMEM_STATE_ROOT."
    }
    $observed = @()
    foreach ($line in Get-Content -LiteralPath $envPath) {
        if ($line -match '^\s*EXOMEM_STATE_ROOT\s*=\s*(.*)\s*$') {
            $value = $Matches[1].Trim()
            if (($value.StartsWith('"') -and $value.EndsWith('"')) -or ($value.StartsWith("'") -and $value.EndsWith("'"))) {
                $value = $value.Substring(1, $value.Length - 2)
            }
            $observed += $value
        }
    }
    if ($observed.Count -gt 1 -and @($observed | Select-Object -Unique).Count -ne 1) {
        throw "The managed dotenv contains conflicting EXOMEM_STATE_ROOT bindings."
    }
    if (-not $observed.Count) { return "" }
    $bound = [string]$observed[-1]
    if (-not [System.IO.Path]::IsPathFullyQualified($bound)) {
        throw "The managed EXOMEM_STATE_ROOT binding must be absolute."
    }
    return [System.IO.Path]::GetFullPath($bound)
}

function Resolve-ExomemDotenvStateRootBinding {
    <# Select the sticky binding without writing it. #>
    param(
        [string]$AppDirectory,
        [string]$PreferredRoot = ""
    )

    $existing = Get-ExomemDotenvStateRootBinding -AppDirectory $AppDirectory
    $selected = if ($existing) { $existing } elseif ($PreferredRoot) { $PreferredRoot } else { Get-ExomemDefaultManagedStateRoot }
    if (-not [System.IO.Path]::IsPathFullyQualified($selected)) {
        throw "The managed EXOMEM_STATE_ROOT binding must be absolute."
    }
    return [System.IO.Path]::GetFullPath($selected)
}

function Ensure-ExomemDotenvStateRootBinding {
    <#
      Persist one no-secret-output state-root authority in the service dotenv.
      python-dotenv loads AppDirectory/.env with override=True before readiness,
      so this exact value is shared by LocalSystem and the operator-run target
      interpreter without writing the admin-only NSSM registry key.
    #>
    param(
        [string]$AppDirectory,
        [string]$ExpectedStateRoot = ""
    )

    $envPath = Join-Path $AppDirectory ".env"
    $existing = Get-ExomemDotenvStateRootBinding -AppDirectory $AppDirectory
    $bound = Resolve-ExomemDotenvStateRootBinding -AppDirectory $AppDirectory -PreferredRoot $ExpectedStateRoot
    if ($ExpectedStateRoot) {
        $expected = [System.IO.Path]::GetFullPath($ExpectedStateRoot)
        if ($bound -ne $expected) {
            throw "The managed EXOMEM_STATE_ROOT binding does not match the durable transition receipt."
        }
    }
    if (-not $existing) {
        $raw = [System.IO.File]::ReadAllText($envPath)
        $prefix = if ($raw.Length -eq 0 -or $raw.EndsWith("`n") -or $raw.EndsWith("`r")) { "" } else { "`r`n" }
        [System.IO.File]::AppendAllText(
            $envPath,
            "${prefix}EXOMEM_STATE_ROOT=$bound`r`n",
            [System.Text.UTF8Encoding]::new($false)
        )
    }
    return $bound
}

function Get-ExomemServiceRoot {
    <#
    .SYNOPSIS
      Given <root>\.venv\Scripts\python.exe, return <root>. $null if it doesn't match.
    .DESCRIPTION
      Lets install/upgrade re-target the venv the service already uses instead of
      guessing a directory name. This is what keeps a box installed at
      'exomem-service-ha' from being silently re-provisioned into the
      'exomem-service-release' default.
    #>
    param([string]$PythonPath)

    if (-not $PythonPath) { return $null }
    $scripts = Split-Path -Parent $PythonPath           # ...\.venv\Scripts
    if (-not $scripts) { return $null }
    $venv = Split-Path -Parent $scripts                 # ...\.venv
    if (-not $venv) { return $null }
    if ((Split-Path -Leaf $venv) -ne ".venv") { return $null }
    return Split-Path -Parent $venv                     # ...\<root>
}

function Get-ExomemInstalledVersion {
    <#
    .SYNOPSIS
      Return the exomem version installed in a given interpreter, or $null.
    #>
    param([string]$PythonPath)

    if (-not $PythonPath -or -not (Test-Path $PythonPath)) { return $null }
    $out = & $PythonPath -c "import importlib.metadata as m; print(m.version('exomem'))" 2>$null
    if ($LASTEXITCODE -ne 0) { return $null }
    $version = ($out | Select-Object -First 1)
    if (-not $version) { return $null }
    return $version.Trim()
}

function Get-ExomemDotenvValue {
    <#
    .SYNOPSIS
      Read a single key out of <repo>\.env, or $null.
    #>
    param(
        [string]$RepoRoot,
        [string]$Name
    )

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

function Get-ExomemServiceEndpoint {
    <#
    .SYNOPSIS
      Return @{ Host; Port } parsed from the service's registered AppParameters.
    .DESCRIPTION
      Reads the actual --host/--port the service was installed with rather than
      assuming the defaults, so health checks probe the right socket. Falls back to
      127.0.0.1:8765 (the install default) when the key can't be read. A wildcard
      bind is rewritten to loopback because you can't connect to 0.0.0.0.
    #>
    param([string]$ServiceName)

    $result = @{ Host = "127.0.0.1"; Port = 8765 }
    $key = "HKLM:\SYSTEM\CurrentControlSet\Services\$ServiceName\Parameters"
    try {
        $params = Get-ItemProperty -Path $key -ErrorAction Stop
    } catch {
        return $result
    }
    if (-not ($params.PSObject.Properties.Name -contains "AppParameters")) { return $result }

    $appParams = [string]$params.AppParameters
    if ($appParams -match '--host\s+(\S+)') {
        $parsed = $Matches[1]
        if ($parsed -in @("0.0.0.0", "::", "[::]")) { $parsed = "127.0.0.1" }
        $result.Host = $parsed
    }
    if ($appParams -match '--port\s+(\d+)') { $result.Port = [int]$Matches[1] }
    return $result
}

function Assert-ExomemPowerShell7 {
    <#
    .SYNOPSIS
      Refuse to run under Windows PowerShell 5.1, which cannot execute these scripts.
    .DESCRIPTION
      Not a style preference: the deploy path calls
      `Invoke-WebRequest -SkipHttpErrorCheck`, which is PowerShell 7.0+ only. Under
      5.1 the run used to die partway with a NativeCommandError on an ordinary `uv`
      banner -- a message that named neither the shell nor the real requirement.
    #>
    param([string]$ScriptName)

    if ($PSVersionTable.PSVersion.Major -ge 7) { return }
    throw "$ScriptName requires PowerShell 7+ (pwsh); this is Windows PowerShell $($PSVersionTable.PSVersion). Re-run with: pwsh -File scripts/$ScriptName"
}

function Invoke-ExomemNative {
    <#
    .SYNOPSIS
      Run a native command, return its exit code and captured output, and never let
      its stderr masquerade as a failure.
    .DESCRIPTION
      `uv` writes its ENTIRE plan to stderr by design -- the "Using Python 3.13.11
      environment at: ..." banner, "Resolved N packages", and the "+ exomem==X"
      lines all arrive there and nothing arrives on stdout. So the streams have to
      be merged with 2>&1 for the output to be logged at all.

      The hazard is what that merge does under `$ErrorActionPreference = "Stop"`,
      which every calling script sets: Windows PowerShell 5.1 raises a terminating
      NativeCommandError on the FIRST merged stderr record, even when the command
      exits 0. That turned an informational banner into an aborted deploy (#578).

      Both preference assignments below are function-scoped -- PowerShell shadows
      the caller's value for this frame only and restores it on return -- so the
      calling script keeps "Stop" semantics everywhere else. $ErrorActionPreference
      keeps a stderr line from being fatal; $PSNativeCommandUseErrorActionPreference
      (7.3+; a harmless unused variable on 5.1) keeps a NONZERO EXIT from throwing
      before the code can be returned, which is what leaves "uv failed" reportable
      as something other than "uv did nothing".
    #>
    param(
        [string[]]$CommandArgs,
        [switch]$Quiet
    )

    $ErrorActionPreference = "Continue"
    $PSNativeCommandUseErrorActionPreference = $false

    # Without this, a missing executable leaves $LASTEXITCODE at whatever the
    # previous command set -- readable as success.
    if (-not (Get-Command $CommandArgs[0] -ErrorAction SilentlyContinue)) {
        throw "Required command '$($CommandArgs[0])' was not found on PATH."
    }

    $merged = & $CommandArgs[0] @($CommandArgs[1..($CommandArgs.Count - 1)]) 2>&1
    $code = $LASTEXITCODE
    $lines = @(foreach ($item in $merged) {
        if ($item -is [System.Management.Automation.ErrorRecord]) { $item.ToString() } else { [string]$item }
    })
    if (-not $Quiet) { foreach ($line in $lines) { Write-Host $line } }
    return @{ ExitCode = $code; Lines = $lines }
}

function Invoke-LoggedNative {
    <#
    .SYNOPSIS
      Run a native command, echo its output, and return its exit code.
    #>
    param([string[]]$CommandArgs)

    return (Invoke-ExomemNative -CommandArgs $CommandArgs).ExitCode
}

function Get-ExomemPackageSpec {
    <#
    .SYNOPSIS
      Map a doctor profile to the PyPI requirement string, with optional pin.
    #>
    param(
        [string]$Profile,
        [string]$PackageVersion = ""
    )

    $extras = switch ($Profile) {
        "hybrid"   { "[embeddings]" }
        "standard" { "[embeddings,media]" }
        "media"    { "[embeddings,media,vision,diarization]" }
        default    { "" }                      # lean
    }
    $pin = if ($PackageVersion) { "==$PackageVersion" } else { "" }
    return "exomem$extras$pin"
}

function Get-ExomemInstallPlanVersion {
    <#
    .SYNOPSIS
      Pull the exomem version out of a `uv pip install` plan, or $null.
    .DESCRIPTION
      uv prints its plan as " + exomem==0.52.3" / " - exomem==0.52.2" (extras are
      normalised away, so the name is bare). It prints NO "+ exomem" line when it
      would install nothing -- "Would make no changes" / "Audited N packages" --
      which the caller reads as "the resolved target is what is already there".
    #>
    param([string[]]$Lines)

    foreach ($line in $Lines) {
        if ($line -match '^\s*\+\s*exomem==(\S+)\s*$') { return $Matches[1] }
    }
    return $null
}

function Resolve-ExomemTargetVersion {
    <#
    .SYNOPSIS
      Return the concrete version an install of $PackageSpec would land, or $null.
    .DESCRIPTION
      `--dry-run` resolves without writing, so the operator can be told which
      version the run is about to land BEFORE it lands, and the post-install
      assertion has something concrete to compare against even when no
      -PackageVersion was pinned.

      `--refresh-package exomem` is load-bearing, not belt-and-braces: uv serves the
      package index out of its HTTP cache, so an unpinned `--upgrade` can resolve to
      a release that is no longer the latest and exit 0 having done nothing. A
      deploy script must not inherit that, and a target read through a stale cache
      would agree with the stale install and vouch for it.
    #>
    param(
        [string]$Python,
        [string]$PackageSpec,
        [string]$Installed = ""
    )

    $result = Invoke-ExomemNative -Quiet -CommandArgs @(
        "uv", "pip", "install", "--dry-run", "--upgrade",
        "--refresh-package", "exomem", "--python", $Python, $PackageSpec
    )
    if ($result.ExitCode -ne 0) {
        Write-Warning "Could not resolve the target version for $PackageSpec (uv --dry-run exited $($result.ExitCode)):"
        foreach ($line in $result.Lines) { Write-Host "  uv: $line" }
        return $null
    }
    $planned = Get-ExomemInstallPlanVersion -Lines $result.Lines
    if ($planned) { return $planned }
    # uv planned no change to exomem, so the resolved target is the installed one.
    if ($Installed) { return $Installed }
    return $null
}

function Assert-ExomemInstallApplied {
    <#
    .SYNOPSIS
      Fail when an install reported success without changing what is installed.
    .DESCRIPTION
      #578: `uv` exited 0 having installed nothing, the script printed
      "Installed version: 0.52.2 -> 0.52.2" as a REPORT, and the deploy continued.
      The service restarted cleanly on the old build and every later observation
      was attributed to a release that was never deployed. before/after were
      already known; this turns the report into a gate.
    #>
    param(
        [string]$PackageSpec,
        [string]$Before = "",
        [string]$After = "",
        [string]$Target = ""
    )

    $was = if ($Before) { $Before } else { "not installed" }
    if (-not $After) {
        throw "Install of '$PackageSpec' reported success but no exomem version is importable from that interpreter (was: $was). Nothing was deployed."
    }
    if (-not $Target) {
        Write-Warning "Target version unresolved, so this run can only assert that SOMETHING is installed ($After). Re-run with -PackageVersion <version> for a checked upgrade."
        return
    }
    if ($After -ne $Target) {
        throw "Install did not take: '$PackageSpec' resolved to $Target, but the interpreter still reports $After (was: $was). uv exited 0 without applying the change; re-run with -PackageVersion $Target, and check that uv is not resolving from a stale index cache."
    }
}

function Install-ExomemPackage {
    <#
    .SYNOPSIS
      Install/upgrade exomem into an existing interpreter. Throws on failure, and
      on a "success" that left the interpreter on the version it started on.
    #>
    param(
        [string]$Python,
        [string]$Profile,
        [string]$PackageVersion = ""
    )

    $pkg = Get-ExomemPackageSpec -Profile $Profile -PackageVersion $PackageVersion
    $before = Get-ExomemInstalledVersion -PythonPath $Python
    $target = Resolve-ExomemTargetVersion -Python $Python -PackageSpec $pkg -Installed $before
    # A pin stays assertable even when the resolve could not run at all.
    if (-not $target -and $PackageVersion) { $target = $PackageVersion }
    Write-Host "  target:    $(if ($target) { $target } else { 'unresolved' })"

    Write-Host "Installing $pkg into $Python..."
    $code = Invoke-LoggedNative @("uv", "pip", "install", "--upgrade", "--refresh-package", "exomem", "--python", $Python, $pkg)
    if ($code -ne 0) { throw "uv pip install failed for $pkg (uv exit $code)" }

    $after = Get-ExomemInstalledVersion -PythonPath $Python
    Assert-ExomemInstallApplied -PackageSpec $pkg -Before $before -After $after -Target $target
}

function Repair-TorchCuda {
    <#
    .SYNOPSIS
      Restore the CUDA torch build that a plain `uv pip install` silently replaces.
    .DESCRIPTION
      `uv pip` (unlike `uv sync`) does NOT consult [tool.uv.sources], so installing
      exomem resolves torch from PyPI -- a CPU wheel -- clobbering the CUDA build and
      silently moving embeddings/media onto the CPU. The same hazard is documented in
      the Dockerfile.

      Reinstalls the SAME version the resolver chose, from the CUDA index. It never
      substitutes a different version: an earlier hardcoded pin here went stale and
      began downgrading torch on every upgrade.
    #>
    param(
        [string]$Python,
        [string]$Profile,
        [ValidateSet("auto", "always", "never")]
        [string]$CudaTorch = "auto"
    )

    if ($Profile -eq "lean") { return }
    $shouldCuda = switch ($CudaTorch) {
        "always" { $true }
        "never"  { $false }
        default  { [bool](Get-Command nvidia-smi -ErrorAction SilentlyContinue) }
    }
    if (-not $shouldCuda) { return }

    $installed = & $Python -c "import torch; print(torch.__version__)" 2>$null
    if ($LASTEXITCODE -ne 0) { $installed = $null }
    $installed = if ($installed) { ($installed | Select-Object -First 1).Trim() } else { $null }

    if (-not $installed) {
        Write-Host "Torch is not installed in this venv; nothing to repair."
        return
    }
    if ($installed -match '\+cu') {
        Write-Host "CUDA Torch already present ($installed); leaving it alone."
        return
    }

    $target = "torch==$(($installed -split '\+')[0])+cu132"
    Write-Host "Replacing CPU Torch ($installed) with the CUDA 13.2 build ($target)..."
    $code = Invoke-LoggedNative @(
        "uv", "pip", "install", "--python", $Python,
        "--default-index", "https://download.pytorch.org/whl/cu132", $target
    )
    if ($code -ne 0) {
        throw "$target is not available on https://download.pytorch.org/whl/cu132. The service would run on CPU. Pin a torch version that has a cu132 build, or pass -CudaTorch never to accept CPU deliberately."
    }

    # Assert the swap took. A CPU wheel here is the exact silent GPU regression this
    # function exists to prevent, so fail loudly rather than reporting success.
    $verify = & $Python -c "import torch; print(torch.__version__, torch.cuda.is_available())" 2>$null
    Write-Host "  torch now: $verify"
    if ($verify -notmatch '\+cu') {
        throw "CUDA Torch install reported success but torch is still '$verify'."
    }
}

function Get-ExomemRepoVersion {
    <#
    .SYNOPSIS
      Return the version declared in the repo's pyproject.toml, or $null.
    .DESCRIPTION
      Deliberately offline. Comparing the service against the repo (rather than
      PyPI) keeps every gate usable on a disconnected box and preserves doctor's
      offline-by-contract design.
    #>
    param([string]$RepoRoot)

    $pyproject = Join-Path $RepoRoot "pyproject.toml"
    if (-not (Test-Path $pyproject)) { return $null }
    foreach ($line in Get-Content $pyproject) {
        if ($line -match '^\s*version\s*=\s*"([^"]+)"') { return $Matches[1] }
    }
    return $null
}

function Test-ExomemUvToolInstall {
    if (-not (Get-Command uv -ErrorAction SilentlyContinue)) { return $false }
    $listing = & uv tool list 2>$null
    if ($LASTEXITCODE -ne 0) { return $false }
    return [bool]($listing | Where-Object { $_ -match '^exomem(?:\s|$)' } | Select-Object -First 1)
}

function Sync-ExomemUvCli {
    <# Align only the lean uv-tool command; the service keeps its selected extras. #>
    param(
        [ValidateSet("auto", "always", "never")]
        [string]$Mode,
        [string]$ServiceVersion
    )

    if ($Mode -eq "never") {
        Write-Host "CLI sync disabled (-CliSync never)."
        return $false
    }
    if ($Mode -eq "auto" -and -not (Test-ExomemUvToolInstall)) {
        Write-Host "No existing uv-managed Exomem CLI; auto mode will not install one."
        return $false
    }
    if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
        throw "uv is required for CLI sync. Install uv or pass -CliSync never."
    }
    if (-not $ServiceVersion) {
        throw "Cannot sync the CLI without a verified live service version."
    }
    Write-Host "Aligning lean uv-tool CLI to exomem==$ServiceVersion..."
    $code = Invoke-LoggedNative @("uv", "tool", "install", "--force", "exomem==$ServiceVersion")
    if ($code -ne 0) { throw "uv tool CLI sync failed for exomem==$ServiceVersion" }
    return $true
}

function Get-ExomemManagedManifestPath {
    if ($env:EXOMEM_MANAGED_INSTALL_MANIFEST) {
        return $env:EXOMEM_MANAGED_INSTALL_MANIFEST
    }
    $root = if ($env:LOCALAPPDATA) {
        Join-Path $env:LOCALAPPDATA "Exomem"
    } else {
        Join-Path ([Environment]::GetFolderPath("LocalApplicationData")) "Exomem"
    }
    return Join-Path $root "managed-install.json"
}

function Write-ExomemManagedManifest {
    param(
        [string]$ServiceVersion,
        [string]$ServiceProfile,
        [string]$ServiceTarget
    )

    $path = Get-ExomemManagedManifestPath
    $parent = Split-Path -Parent $path
    if (-not (Test-Path $parent)) { New-Item -ItemType Directory -Path $parent -Force | Out-Null }
    $payload = [ordered]@{
        schema_version = 1
        service_version = $ServiceVersion
        service_profile = $ServiceProfile
        service_target = $ServiceTarget
        cli_profile = "lean"
        cli_route = "direct"
    }
    $temporary = "$path.$PID.tmp"
    $payload | ConvertTo-Json | Set-Content -Path $temporary -Encoding utf8
    Move-Item -LiteralPath $temporary -Destination $path -Force
    Write-Host "Managed install manifest: $path"
}

function Backup-ExomemAppLog {
    <#
    .SYNOPSIS
      Archive logs/exomem.log to logs/archive/ (keep newest N) instead of
      deleting it outright, so a restart doesn't erase the previous session's
      diagnostics before anyone reads them.
    #>
    param(
        [string]$LogPath,
        [string]$ArchiveDir,
        [int]$Keep = 10
    )

    if (-not (Test-Path $LogPath)) { return }
    if (-not (Test-Path $ArchiveDir)) {
        New-Item -ItemType Directory -Path $ArchiveDir -Force | Out-Null
    }
    $stamp = Get-Date -Format "yyyyMMdd-HHmmss"
    $dest = Join-Path $ArchiveDir "exomem-$stamp.log"
    Move-Item -LiteralPath $LogPath -Destination $dest -Force
    $archives = @(
        Get-ChildItem -Path $ArchiveDir -Filter "exomem-*.log" -ErrorAction SilentlyContinue |
            Sort-Object LastWriteTime -Descending
    )
    if ($archives.Count -gt $Keep) {
        $archives | Select-Object -Skip $Keep | Remove-Item -Force -ErrorAction SilentlyContinue
    }
}

function Limit-ExomemServiceLogPile {
    <#
    .SYNOPSIS
      Prune NSSM's online-rotated service.out.log.*/service.err.log.* pile,
      keeping the newest N of each stream. NSSM's AppRotateOnline renames the
      live file on every rotation and never caps how many accumulate.
    #>
    param(
        [string]$LogDir,
        [int]$Keep = 20
    )

    foreach ($pattern in @("service.out.log.*", "service.err.log.*")) {
        $files = @(
            Get-ChildItem -Path $LogDir -Filter $pattern -ErrorAction SilentlyContinue |
                Sort-Object LastWriteTime -Descending
        )
        if ($files.Count -gt $Keep) {
            $files | Select-Object -Skip $Keep | Remove-Item -Force -ErrorAction SilentlyContinue
        }
    }
}

function Assert-ExomemVisibleCliVersions {
    param(
        [string]$ExpectedVersion,
        [bool]$RequireOne = $false
    )

    $commands = @(
        Get-Command exomem, kb -All -CommandType Application -ErrorAction SilentlyContinue |
            Select-Object -ExpandProperty Source -Unique
    )
    if ($RequireOne -and $commands.Count -eq 0) {
        throw "CLI sync completed but neither exomem nor kb is visible on PATH. Run 'uv tool update-shell', open a new shell, and retry."
    }
    foreach ($executable in $commands) {
        $raw = & $executable --version --json 2>$null
        if ($LASTEXITCODE -ne 0 -or -not $raw) {
            throw "CLI verification failed: '$executable' does not support --version --json. Repair with: uv tool install --force exomem==$ExpectedVersion"
        }
        try {
            $identity = ($raw -join "`n") | ConvertFrom-Json -ErrorAction Stop
        } catch {
            throw "CLI verification failed: '$executable' returned invalid version JSON. Repair with: uv tool install --force exomem==$ExpectedVersion"
        }
        if ($identity.version -ne $ExpectedVersion) {
            throw "CLI/service split: '$executable' reports '$($identity.version)' while the live service reports '$ExpectedVersion'. Repair with: uv tool install --force exomem==$ExpectedVersion"
        }
        Write-Host "Verified $executable -> exomem $($identity.version)"
    }
}

function Get-ExomemListenerPidsForPort {
    param([int]$Port)

    try {
        $netstat = Get-Command netstat -ErrorAction Stop
    } catch {
        throw "Listener enumeration for port $Port is unavailable: $($_.Exception.Message)"
    }

    $previousExitCode = $global:LASTEXITCODE
    $global:LASTEXITCODE = 0
    try {
        $lines = @(& $netstat -ano -p tcp 2>$null)
        $exitCode = $global:LASTEXITCODE
    } catch {
        throw "Listener enumeration for port $Port failed: $($_.Exception.Message)"
    } finally {
        $global:LASTEXITCODE = $previousExitCode
    }
    if ($netstat.CommandType -eq 'Application' -and $exitCode -ne 0) {
        throw "Listener enumeration for port $Port failed with exit code $exitCode."
    }

    $pids = @()
    foreach ($line in $lines) {
        $fields = @(([string]$line).Trim() -split '\s+')
        if ($fields.Count -lt 2 -or $fields[0] -ne 'TCP') { continue }
        if ($fields[1] -notmatch ":$Port$") { continue }
        if ($fields.Count -lt 4 -or $fields[3] -ne 'LISTENING') { continue }
        $candidate = 0
        if ($fields.Count -lt 5 -or -not [int]::TryParse($fields[-1], [ref]$candidate) -or $candidate -le 0) {
            throw "Listener enumeration for port $Port found a LISTENING socket without an attributable process id."
        }
        $pids += $candidate
    }
    return @($pids | Sort-Object -Unique)
}

function Get-ExomemConfiguredListenerPids {
    param([string]$ServiceName = "exomem")

    $endpoint = Get-ExomemServiceEndpoint -ServiceName $ServiceName
    return @(Get-ExomemListenerPidsForPort -Port $endpoint.Port)
}

$script:ExomemTransitionReceiptTool = Join-Path $PSScriptRoot "service-transition-receipt.py"

function Get-ExomemTransitionReceiptPath {
    param([string]$ServiceName = "exomem")

    if ($ServiceName -notmatch '^[A-Za-z0-9_.@-]+$') {
        throw "Service identity is invalid for a transition receipt."
    }
    $root = if ($env:EXOMEM_TRANSITION_RECEIPT_ROOT) {
        $env:EXOMEM_TRANSITION_RECEIPT_ROOT
    } else {
        Join-Path $env:LOCALAPPDATA "exomem\transitions"
    }
    if (-not [System.IO.Path]::IsPathFullyQualified($root)) {
        throw "Transition receipt root must be absolute."
    }
    return Join-Path ([System.IO.Path]::GetFullPath($root)) "$ServiceName.json"
}

function Invoke-ExomemTransitionReceiptTool {
    param(
        [string]$PythonPath,
        [string[]]$Arguments
    )

    if (-not (Test-Path -LiteralPath $script:ExomemTransitionReceiptTool -PathType Leaf)) {
        throw "Transition receipt tool is missing."
    }
    $command = @($PythonPath, $script:ExomemTransitionReceiptTool) + $Arguments
    $result = Invoke-ExomemNative -Quiet -CommandArgs $command
    if ($result.ExitCode -ne 0) {
        $detail = ($result.Lines -join " ").Trim()
        if (-not $detail) { $detail = "transition receipt operation failed" }
        throw $detail
    }
    return @($result.Lines)
}

function Get-ExomemTransitionReceiptIdentityArguments {
    param(
        [string]$ServiceName,
        [string]$BindingPath,
        [string]$StateRoot,
        [string]$VaultPath,
        [int]$TargetPort
    )

    return @(
        "--path", (Get-ExomemTransitionReceiptPath -ServiceName $ServiceName),
        "--service-id", $ServiceName,
        "--binding-path", $BindingPath,
        "--state-root", $StateRoot,
        "--vault", $VaultPath,
        "--target-port", [string]$TargetPort
    )
}

function New-ExomemTransitionReceipt {
    param(
        [string]$PythonPath,
        [string]$ServiceName,
        [string]$BindingPath,
        [string]$StateRoot,
        [string]$VaultPath,
        [int]$Port,
        [int]$TargetPort,
        [int]$WorkerPid,
        [int[]]$ListenerPids = @()
    )

    $identityParameters = @{
        ServiceName = $ServiceName; BindingPath = $BindingPath; StateRoot = $StateRoot
        VaultPath = $VaultPath; TargetPort = $TargetPort
    }
    $identity = Get-ExomemTransitionReceiptIdentityArguments @identityParameters
    $arguments = @("create") + $identity + @(
        "--port", [string]$Port, "--worker-pid", [string]$WorkerPid
    )
    foreach ($listenerPid in @($ListenerPids)) {
        $arguments += @("--listener-pid", [string]$listenerPid)
    }
    Invoke-ExomemTransitionReceiptTool -PythonPath $PythonPath -Arguments $arguments | Out-Null
    return Read-ExomemTransitionReceipt -PythonPath $PythonPath @identityParameters
}

function Read-ExomemTransitionReceipt {
    param(
        [string]$PythonPath,
        [string]$ServiceName,
        [string]$BindingPath,
        [string]$StateRoot,
        [string]$VaultPath,
        [int]$TargetPort
    )

    $identity = Get-ExomemTransitionReceiptIdentityArguments `
        -ServiceName $ServiceName `
        -BindingPath $BindingPath `
        -StateRoot $StateRoot `
        -VaultPath $VaultPath `
        -TargetPort $TargetPort
    $lines = Invoke-ExomemTransitionReceiptTool -PythonPath $PythonPath -Arguments (@("verify") + $identity + @("--json"))
    try {
        return (($lines -join "`n") | ConvertFrom-Json -ErrorAction Stop)
    } catch {
        throw "Transition receipt verifier returned invalid JSON."
    }
}

function Set-ExomemTransitionReceiptPhase {
    param(
        [string]$PythonPath,
        [string]$ServiceName,
        [string]$BindingPath,
        [string]$StateRoot,
        [string]$VaultPath,
        [int]$TargetPort,
        [string]$Phase,
        [int[]]$ObservedPids = @()
    )

    $identityParameters = @{
        ServiceName = $ServiceName; BindingPath = $BindingPath; StateRoot = $StateRoot
        VaultPath = $VaultPath; TargetPort = $TargetPort
    }
    $arguments = @("phase") + (Get-ExomemTransitionReceiptIdentityArguments @identityParameters) + @("--phase", $Phase)
    foreach ($observedPid in @($ObservedPids)) {
        if ($observedPid) { $arguments += @("--observed-pid", [string]$observedPid) }
    }
    Invoke-ExomemTransitionReceiptTool -PythonPath $PythonPath -Arguments $arguments | Out-Null
}

function Publish-ExomemFailedTransitionReceipt {
    param(
        [string]$PythonPath,
        [string]$ServiceName,
        [string]$BindingPath,
        [string]$StateRoot,
        [string]$VaultPath,
        [int]$TargetPort,
        [int]$ObservedWorkerPid = 0
    )

    $identity = @{
        PythonPath = $PythonPath; ServiceName = $ServiceName; BindingPath = $BindingPath
        StateRoot = $StateRoot; VaultPath = $VaultPath; TargetPort = $TargetPort
    }
    $receipt = Read-ExomemTransitionReceipt @identity
    $observedPids = @(@($ObservedWorkerPid) | Where-Object { $_ })
    if (@("starting", "started") -contains [string]$receipt.phase) {
        $proofPhase = [string]$receipt.phase
        foreach ($port in @($receipt.port, $receipt.target_port) | Where-Object { $_ } | Select-Object -Unique) {
            $observedPids += @(Get-ExomemListenerPidsForPort -Port $port)
        }
        $observedPids = @($observedPids | Where-Object { $_ } | Select-Object -Unique)
        Set-ExomemTransitionReceiptPhase @identity -Phase $proofPhase -ObservedPids $observedPids
        $receipt = Read-ExomemTransitionReceipt @identity
        foreach ($observedPid in $observedPids) {
            if (@($receipt.proof_pids) -notcontains $observedPid) {
                throw "The durable failed-start process proof is incomplete; the receipt remains non-resumable."
            }
        }
        Set-ExomemTransitionReceiptPhase @identity -Phase "failed"
    } else {
        $observedPids = @($observedPids | Where-Object { $_ } | Select-Object -Unique)
        Set-ExomemTransitionReceiptPhase @identity -Phase "failed" -ObservedPids $observedPids
    }
    return Read-ExomemTransitionReceipt @identity
}

function Remove-ExomemTransitionReceipt {
    param(
        [string]$PythonPath,
        [string]$ServiceName,
        [string]$BindingPath,
        [string]$StateRoot,
        [string]$VaultPath,
        [int]$TargetPort
    )

    $identity = Get-ExomemTransitionReceiptIdentityArguments `
        -ServiceName $ServiceName `
        -BindingPath $BindingPath `
        -StateRoot $StateRoot `
        -VaultPath $VaultPath `
        -TargetPort $TargetPort
    Invoke-ExomemTransitionReceiptTool -PythonPath $PythonPath -Arguments (@("clear") + $identity) | Out-Null
}

function Get-ExomemServiceWorkerPid {
    <#
    .SYNOPSIS
      Return the pid of the interpreter the service actually runs, or 0.
    .DESCRIPTION
      NSSM is the service process; the Python worker is its child, and the child
      is what holds loaded code. Returns 0 rather than throwing whenever the
      service is stopped, the platform is not Windows, or CIM is unavailable, so
      callers can treat 0 as "no baseline" instead of handling an exception.
    #>
    param([string]$ServiceName = "exomem")

    if ($env:OS -ne "Windows_NT") { return 0 }
    $childPid = 0
    try {
        $service = Get-CimInstance Win32_Service -Filter "Name='$ServiceName'" -ErrorAction Stop
    } catch {
        $service = $null
    }
    if ($service -and $service.ProcessId) {
        try {
            $child = Get-CimInstance Win32_Process -Filter "ParentProcessId=$($service.ProcessId)" -ErrorAction Stop |
                Select-Object -First 1
            if ($child) { $childPid = [int]$child.ProcessId }
        } catch {
            $childPid = 0
        }
    }
    if ($childPid) { return $childPid }

    # Ordinary deploy tokens may read the service registry and control the SCM
    # while CIM child enumeration is denied. The bound TCP listener remains a
    # closed observable: a unique pid on the service's configured port is the
    # worker to capture. Ambiguous or unreadable output stays a refusal (0).
    try {
        $pids = @(Get-ExomemConfiguredListenerPids -ServiceName $ServiceName)
    } catch {
        return 0
    }
    if ($pids.Count -ne 1) { return 0 }
    return [int]$pids[0]
}

function Assert-ExomemServiceStopped {
    <#
    .SYNOPSIS
      Prove that the service is stopped and its Python worker pid is gone.
    .DESCRIPTION
      The state-root migration lock is invisible to legacy releases.  A
      successful stop command is therefore not migration authority: the SCM
      state and the actual child process must both be absent before any wheel
      is replaced or legacy state is copied.
    #>
    param(
        [string]$ServiceName = "exomem",
        [int]$Before = 0,
        [int]$After = 0,
        [int[]]$CapturedPids = @(),
        [int[]]$Ports = @()
    )

    $service = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
    if (-not $service -or $service.Status -ne 'Stopped') {
        $observed = if ($service) { [string]$service.Status } else { "missing" }
        throw "Service '$ServiceName' is not proven stopped (SCM state: $observed). Offline state migration is refused."
    }
    if (-not $CapturedPids.Count) {
        $CapturedPids = @($Before, $After) | Where-Object { $_ } | Select-Object -Unique
    }
    if (-not $CapturedPids.Count) {
        throw "Service '$ServiceName' has no captured pre-stop worker pid. Offline state migration cannot prove the legacy writer is gone."
    }
    foreach ($captured in @($CapturedPids) | Where-Object { $_ } | Select-Object -Unique) {
        $oldWorker = Get-Process -Id $captured -ErrorAction SilentlyContinue
        if ($oldWorker) {
            throw "Service '$ServiceName' reports stopped but captured worker pid $captured is still alive. Offline state migration is refused."
        }
    }
    foreach ($port in @($Ports) | Where-Object { $_ } | Select-Object -Unique) {
        $listeners = @(Get-ExomemListenerPidsForPort -Port $port)
        if ($listeners.Count) {
            throw "Service '$ServiceName' is stopped but listener port $port is still owned by pid(s): $($listeners -join ', '). Offline state migration is refused."
        }
    }
    Write-Host "Service stopped and every captured process pid is gone: $ServiceName"
}

function Assert-ExomemStoppedResumeAuthority {
    <# Prove an explicitly requested recovery of a transition already left stopped. #>
    param(
        [string]$ServiceName = "exomem",
        [Parameter(Mandatory = $true)]
        [object]$Receipt
    )

    $service = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
    if (-not $service -or $service.Status -ne 'Stopped') {
        $observed = if ($service) { [string]$service.Status } else { "missing" }
        throw "Service '$ServiceName' is not proven stopped (SCM state: $observed). Stopped-transition recovery is refused."
    }
    if (-not $Receipt -or $Receipt.service_id -ne $ServiceName) {
        throw "An exact durable transition receipt is required. Stopped-transition recovery is refused."
    }
    if (@("starting", "started") -contains [string]$Receipt.phase) {
        throw "The transition receipt records an incomplete start with no complete process proof. Stopped-transition recovery is refused."
    }
    foreach ($captured in @($Receipt.proof_pids) | Select-Object -Unique) {
        if ($captured -and (Get-Process -Id $captured -ErrorAction SilentlyContinue)) {
            throw "Service '$ServiceName' reports stopped but captured transition pid $captured is still alive. Stopped-transition recovery is refused."
        }
    }
    foreach ($port in @($Receipt.port, $Receipt.target_port) | Where-Object { $_ } | Select-Object -Unique) {
        $listeners = @(Get-ExomemListenerPidsForPort -Port $port)
        if ($listeners.Count -ne 0) {
            throw "Service '$ServiceName' is stopped but listener port $port is still owned by pid(s): $($listeners -join ', '). Stopped-transition recovery is refused."
        }
    }
    Write-Host "Explicit stopped-transition recovery authority proven: $ServiceName"
}

function Test-ExomemProcessTreeMembership {
    <# Prove that CandidatePid is RootPid or one of its descendants. #>
    param(
        [int]$RootPid,
        [int]$CandidatePid,
        [int]$MaxDepth = 64
    )

    if ($RootPid -le 0 -or $CandidatePid -le 0 -or $MaxDepth -le 0) { return $false }
    $currentPid = $CandidatePid
    $childCreatedAt = $null
    $seen = @{}
    foreach ($depth in 1..$MaxDepth) {
        if ($currentPid -le 0 -or $seen.ContainsKey($currentPid)) { return $false }
        $seen[$currentPid] = $true
        try {
            $process = Get-CimInstance Win32_Process `
                -Filter "ProcessId=$currentPid" `
                -ErrorAction Stop | Select-Object -First 1
        } catch {
            return $false
        }
        if (-not $process) { return $false }
        try {
            $createdAt = [datetime]$process.CreationDate
        } catch {
            return $false
        }
        if (-not $createdAt) { return $false }
        if ($childCreatedAt -and $createdAt -gt $childCreatedAt) { return $false }
        if ($currentPid -eq $RootPid) { return $true }
        $childCreatedAt = $createdAt
        $currentPid = [int]$process.ParentProcessId
    }
    return $false
}

function Assert-ExomemListenerOwnedByWorker {
    param(
        [string]$ServiceName = "exomem",
        [int]$WorkerPid,
        [int]$TimeoutSec = 60
    )

    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    do {
        $listeners = @(Get-ExomemConfiguredListenerPids -ServiceName $ServiceName)
        if ($listeners.Count -eq 1) {
            $listenerPid = [int]$listeners[0]
            $currentWorkerPid = Get-ExomemServiceWorkerPid -ServiceName $ServiceName
            $belongsToTree = $listenerPid -eq $WorkerPid -or (
                Test-ExomemProcessTreeMembership -RootPid $WorkerPid -CandidatePid $listenerPid
            )
            if ($currentWorkerPid -eq $WorkerPid -and $belongsToTree) {
                $confirmedWorkerPid = Get-ExomemServiceWorkerPid -ServiceName $ServiceName
                $confirmedListeners = @(Get-ExomemConfiguredListenerPids -ServiceName $ServiceName)
                if (
                    $confirmedWorkerPid -ne $WorkerPid -or
                    $confirmedListeners.Count -ne 1 -or
                    [int]$confirmedListeners[0] -ne $listenerPid
                ) {
                    throw "The configured listener or service worker changed during process-tree ownership proof."
                }
                Write-Host "Configured listener belongs to the selected worker process tree: $WorkerPid -> $listenerPid"
                return
            }
        }
        if ($listeners.Count -ne 0) {
            throw "The configured listener is not owned by the newly selected service worker process tree rooted at pid $WorkerPid."
        }
        Start-Sleep -Milliseconds 250
    } while ((Get-Date) -lt $deadline)
    throw "The newly selected service worker pid $WorkerPid never bound the configured listener."
}

function Wait-ExomemHealthVersion {
    param(
        [string]$HealthUrl,
        [string]$ExpectedVersion,
        [int]$TimeoutSec = 60
    )

    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    $observed = $null
    do {
        try {
            $response = Invoke-RestMethod -Uri $HealthUrl -TimeoutSec 5 -ErrorAction Stop
            $observed = [string]$response.version
            if ($observed -eq $ExpectedVersion) { return $response }
        } catch {
            $observed = $null
        }
        Start-Sleep -Milliseconds 250
    } while ((Get-Date) -lt $deadline)
    throw "The live health version '$observed' does not equal target interpreter version '$ExpectedVersion'."
}

function Assert-ExomemConfiguredPortUnbound {
    param(
        [string]$ServiceName = "exomem",
        [int]$Port = 0
    )

    $listeners = if ($Port) {
        @(Get-ExomemListenerPidsForPort -Port $Port)
    } else {
        @(Get-ExomemConfiguredListenerPids -ServiceName $ServiceName)
    }
    if ($listeners.Count -ne 0) {
        throw "The configured service listener is already owned by pid(s): $($listeners -join ', '). Offline initialization is refused."
    }
    Write-Host "Configured service listener is unbound: $ServiceName"
}

function Assert-ExomemServiceRestarted {
    <#
    .SYNOPSIS
      Fail when a deploy reports success without the worker process changing.
    .DESCRIPTION
      `/health` reports `importlib.metadata.version("exomem")`, read from disk at
      request time with no relation to the code the running interpreter loaded.
      So when `uv pip install` swapped the wheel under a live process, /health
      served the new version while the interpreter still ran the old one -- and
      worse, a mixed-version process, because modules imported lazily after the
      swap came from the new wheel.

      That makes every version-vs-version gate blind by construction: both
      `install-info --json` and `/health` read the same distribution metadata, so
      comparing them compares disk with disk. The worker's process identity is
      the only observable that separates "restarted onto the new code" from
      "still running the old code".

      A pid of 0 means "not observed". An unknown baseline degrades to a warning,
      because refusing a deploy for lack of a baseline would strand any box where
      the probe is unavailable; a *known* baseline that did not change is fatal.
    #>
    param(
        [int]$Before = 0,
        [int]$After = 0,
        [string]$ServiceName = "exomem"
    )

    if (-not $After) {
        throw "Service '$ServiceName' has no running worker process after the restart. Nothing is serving the deployed version; check logs\service.err.log."
    }
    if (-not $Before) {
        Write-Warning "No pre-restart worker pid was observed, so this run can only assert that a worker is running now (pid $After), not that it restarted onto the new code."
        return
    }
    if ($After -eq $Before) {
        throw "Service '$ServiceName' is still running the same worker process (pid $After) that was live before the upgrade. The wheel on disk changed but the interpreter did not reload it, so /health now reports the new version from disk metadata while the process serves the old code. Restart the service and re-verify."
    }
    Write-Host "Worker process restarted: pid $Before -> $After"
}

function Get-ExomemLogDir {
    <#
    .SYNOPSIS
      Return the log directory the app actually writes to, or $null.
    .DESCRIPTION
      Ask `logging_config.resolve_log_dir()` rather than deriving a second
      constant here. #569 moved logs off `<repo>/logs` for wheel installs, and a
      script that kept its own copy of the old path pruned and tailed a directory
      nothing writes to -- reporting "No log file at ..." while the service
      logged normally somewhere else.

      Resolution runs in this process, so it sees this process's EXOMEM_LOG_DIR.
      That matches the service whenever the variable is unset in both (the common
      case) or set identically; it is the same assumption the doctor check makes.
    #>
    param([string]$PythonPath)

    if (-not $PythonPath -or -not (Test-Path $PythonPath)) { return $null }
    $out = & $PythonPath -c "from exomem.logging_config import resolve_log_dir; print(resolve_log_dir())" 2>$null
    if ($LASTEXITCODE -ne 0) { return $null }
    $dir = ($out | Select-Object -First 1)
    if (-not $dir) { return $null }
    return $dir.Trim()
}

function Test-ExomemAcceleratedTorch {
    <#
    .SYNOPSIS
      True when the torch installed in an interpreter is an accelerator build.
    .DESCRIPTION
      Read the local version tag from distribution metadata, never by importing
      torch: the probe must stay fast and must not fail on a broken ML stack.
      Default PyPI wheels carry no local tag, which on Windows means CPU-only.

      This exists because the deploy accelerator gate read `.accelerated` off
      `install-info --json`, which has never emitted that key. The property was
      always $null, `[bool]$null` is $false, and the guard documented as "a hard
      failure by default" could not fire at all. `/health` does expose it, but
      only while the service is up -- and the gate has to run mid-deploy, so it
      cannot depend on that.
    #>
    param([string]$PythonPath)

    $version = Get-ExomemTorchVersion -PythonPath $PythonPath
    if (-not $version) { return $false }
    foreach ($tag in @("+cu", "+rocm", "+xpu")) {
        if ($version.Contains($tag)) { return $true }
    }
    return $false
}

function Get-ExomemTorchVersion {
    <#
    .SYNOPSIS
      Return the torch version string from distribution metadata, or $null.
    #>
    param([string]$PythonPath)

    if (-not $PythonPath -or -not (Test-Path $PythonPath)) { return $null }
    $out = & $PythonPath -c "import importlib.metadata as m; print(m.version('torch'))" 2>$null
    if ($LASTEXITCODE -ne 0) { return $null }
    $version = ($out | Select-Object -First 1)
    if (-not $version) { return $null }
    return $version.Trim()
}
