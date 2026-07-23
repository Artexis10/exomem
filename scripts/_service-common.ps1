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

function Invoke-LoggedNative {
    <#
    .SYNOPSIS
      Run a native command, echo its output, and return its exit code.
    #>
    param([string[]]$CommandArgs)

    $out = & $CommandArgs[0] @($CommandArgs[1..($CommandArgs.Count - 1)]) 2>&1
    foreach ($line in $out) { Write-Host $line }
    return $LASTEXITCODE
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

function Install-ExomemPackage {
    <#
    .SYNOPSIS
      Install/upgrade exomem into an existing interpreter. Throws on failure.
    #>
    param(
        [string]$Python,
        [string]$Profile,
        [string]$PackageVersion = ""
    )

    $pkg = Get-ExomemPackageSpec -Profile $Profile -PackageVersion $PackageVersion
    Write-Host "Installing $pkg into $Python..."
    $code = Invoke-LoggedNative @("uv", "pip", "install", "--upgrade", "--python", $Python, $pkg)
    if ($code -ne 0) { throw "uv pip install failed for $pkg" }
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
