# Set up ngrok as the public ingress for exomem on this machine, using the free
# static dev domain every ngrok account gets. Run ONCE per machine, AFTER:
#   1. `ngrok config add-authtoken <token>` (claim a token at
#      https://dashboard.ngrok.com/get-started/your-authtoken)
#   2. Claiming a free static domain in the ngrok dashboard:
#      https://dashboard.ngrok.com/domains
#
# What it does (idempotent):
#   - verifies ngrok is on PATH and an authtoken is already configured
#     (`ngrok config check` against your existing ngrok.yml)
#   - writes a small, DEDICATED endpoints-only agent config (version 3) mapping
#     <Domain> -> http://127.0.0.1:<Port>, next to (not overwriting) your
#     existing ngrok.yml -- the authtoken you already configured is untouched;
#     the two files are merged at service-start time via ngrok's own
#     `--config=a,b` merge support, so there is no YAML-merge logic here
#   - installs + starts the ngrok Windows service (auto-start on boot)
#
# Prereqs:
#   - ngrok installed + on PATH  (winget install ngrok.ngrok)
#   - `ngrok config add-authtoken <token>` has been run once (browser step at
#     the dashboard URL above; intentionally NOT automated by this script)
#   - a free static dev domain claimed in the ngrok dashboard
#
# Usage:
#   pwsh -File scripts/setup-ngrok.ps1 -Domain you.ngrok-free.dev
#   pwsh -File scripts/setup-ngrok.ps1 -Domain you.ngrok-free.dev -Port 8765
#
# After this: set EXOMEM_BASE_URL=https://<Domain> in .env, update the GitHub OAuth
# App callback to https://<Domain>/auth/callback, restart exomem, re-add the connector.

param(
    [Parameter(Mandatory = $true)][string]$Domain,
    [int]$Port = 8765
)

$ErrorActionPreference = "Stop"

function Get-NgrokService {
    # The exact registered service name isn't documented; match on name or
    # display name so a future ngrok release naming it differently doesn't
    # silently break idempotency detection.
    Get-Service -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -ieq "ngrok" -or $_.DisplayName -like "*ngrok*" } |
        Select-Object -First 1
}

# --- 1. ngrok must be resolvable -------------------------------------------------------
$ngrokCmd = (Get-Command ngrok -ErrorAction SilentlyContinue)
if (-not $ngrokCmd) {
    Write-Host "ngrok not found on PATH."
    Write-Host "Install it:  winget install ngrok.ngrok"
    exit 1
}
$NgrokExe = $ngrokCmd.Source

# --- 2. An authtoken must already be configured ----------------------------------------
$configCheck = (& $NgrokExe config check 2>&1 | Out-String)
if ($LASTEXITCODE -ne 0 -or $configCheck -notmatch "Valid configuration file") {
    Write-Host "ngrok config check failed:"
    Write-Host $configCheck
    Write-Host ""
    Write-Host "Run 'ngrok config add-authtoken <token>' first."
    Write-Host "Get a token at https://dashboard.ngrok.com/get-started/your-authtoken"
    exit 1
}
Write-Host ($configCheck.Trim())

$pathMatch = [regex]::Match($configCheck, "at (.+)$", [System.Text.RegularExpressions.RegexOptions]::Multiline)
if ($pathMatch.Success) {
    $DefaultConfigPath = $pathMatch.Groups[1].Value.Trim()
} else {
    $DefaultConfigPath = Join-Path $env:LOCALAPPDATA "ngrok\ngrok.yml"
}
if (-not (Test-Path $DefaultConfigPath)) {
    throw "Could not resolve ngrok's default config file (looked for $DefaultConfigPath). Run 'ngrok config add-authtoken <token>' first."
}

# --- 3. Service install needs admin. Self-elevate so behaviour is identical whether ----
#        UAC is on (filtered token) or off.
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Host "Not elevated - relaunching as administrator (approve the UAC prompt)..."
    $hostExe = (Get-Process -Id $PID).Path; if (-not $hostExe) { $hostExe = "pwsh" }
    $relaunchArgs = @(
        "-NoExit", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "`"$PSCommandPath`"",
        "-Domain", $Domain, "-Port", $Port
    )
    Start-Process -FilePath $hostExe -Verb RunAs -ArgumentList $relaunchArgs
    exit
}

# --- 4. Write the dedicated endpoints-only config (never touches your ngrok.yml) -------
$EndpointsConfigPath = Join-Path (Split-Path $DefaultConfigPath -Parent) "exomem-endpoints.yml"
@"
version: 3
endpoints:
  - name: exomem
    url: https://$Domain
    upstream:
      url: http://127.0.0.1:$Port
"@ | Set-Content -Path $EndpointsConfigPath -Encoding ascii
Write-Host "Wrote $EndpointsConfigPath"

# --- 5. Install (or refresh) + start the Windows service -------------------------------
$ConfigArg = "$DefaultConfigPath,$EndpointsConfigPath"

$existingSvc = Get-NgrokService
if ($existingSvc) {
    Write-Host "ngrok service '$($existingSvc.Name)' already installed - reinstalling to pick up config..."
    try { & $NgrokExe service uninstall | Write-Host } catch { Write-Warning "service uninstall: $_" }
    Start-Sleep -Seconds 2
}
Write-Host "Installing ngrok service..."
& $NgrokExe service install --config="$ConfigArg" | Write-Host
Start-Sleep -Seconds 1

$svc = Get-NgrokService
if ($svc) {
    Set-Service -Name $svc.Name -StartupType Automatic
}
try { & $NgrokExe service start | Write-Host } catch { Write-Warning "service start: $_" }
Start-Sleep -Seconds 3

$svc = Get-NgrokService
if (-not $svc -or $svc.Status -ne 'Running') {
    Write-Warning "ngrok service is not Running. See the real error by running it foreground:"
    Write-Warning "  & `"$NgrokExe`" start --all --config=`"$ConfigArg`""
} else {
    Write-Host "Service status: $($svc.Status)"
}

Write-Host ""
Write-Host "Endpoint https://$Domain -> http://127.0.0.1:$Port"
Write-Host ""
Write-Host "NEXT (not automated):"
Write-Host "  1. .env:  EXOMEM_BASE_URL=https://$Domain"
Write-Host "  2. GitHub OAuth App: Homepage https://$Domain ; callback https://$Domain/auth/callback"
Write-Host "  3. Restart exomem:  pwsh -File scripts/restart.ps1"
Write-Host "  4. claude.ai: re-add the connector at https://$Domain/mcp (redo GitHub OAuth)"
Write-Host "  5. Verify the triple:"
Write-Host "       curl.exe -i http://127.0.0.1:$Port/mcp                                (expect 401)"
Write-Host "       curl.exe -i https://$Domain/.well-known/oauth-authorization-server    (expect 200 JSON)"
Write-Host "       curl.exe -i https://$Domain/.well-known/oauth-protected-resource      (expect 200 JSON)"
Write-Host "       exomem doctor --profile remote --probe"
