# Set up a Cloudflare Tunnel as the public ingress for kb-mcp on this machine,
# replacing Tailscale Funnel. Run ONCE per machine, AFTER `cloudflared tunnel login`.
#
# What it does (idempotent):
#   - creates the named tunnel (or reuses it), resolves its UUID
#   - routes the DNS CNAME for <Hostname> to the tunnel
#   - writes config.yml + copies cert.pem/creds into the SYSTEM service location
#     (C:\Windows\System32\config\systemprofile\.cloudflared) — the gotcha that
#     bites everyone: the service runs as SYSTEM and reads config from there, NOT
#     from your %USERPROFILE%\.cloudflared where `tunnel login/create` write.
#   - installs + starts the cloudflared Windows service (auto-start)
#
# Prereqs:
#   - cloudflared installed + on PATH  (winget install --id Cloudflare.cloudflared)
#   - `cloudflared tunnel login`  has been run once (writes cert.pem to your profile,
#     authorizing the substratesystems.io zone). This step is browser-interactive,
#     so it is intentionally NOT in this script.
#
# Usage (desktop):
#   pwsh -File scripts/setup-cloudflared.ps1 -Hostname kb.substratesystems.io -TunnelName kb-mcp-desktop
# Usage (laptop):
#   pwsh -File scripts/setup-cloudflared.ps1 -Hostname kb-laptop.substratesystems.io -TunnelName kb-mcp-laptop
#
# After this: set KB_MCP_BASE_URL=https://<Hostname> in .env, update the GitHub OAuth
# App callback to https://<Hostname>/auth/callback, restart kb-mcp, re-add the connector.

param(
    [Parameter(Mandatory = $true)][string]$Hostname,
    [Parameter(Mandatory = $true)][string]$TunnelName,
    [int]$Port = 8765,
    [string]$CloudflaredPath = "cloudflared"
)

$ErrorActionPreference = "Stop"

# cloudflared must be resolvable.
$cf = (Get-Command $CloudflaredPath -ErrorAction SilentlyContinue)
if (-not $cf) {
    throw "cloudflared not found on PATH. Install it (winget install --id Cloudflare.cloudflared) or pass -CloudflaredPath."
}
$CloudflaredExe = $cf.Source

# The origin cert from `cloudflared tunnel login` lives in the INVOKING user's profile.
# Elevation (below) preserves the same user, so this path stays valid after elevation.
$UserCfDir = Join-Path $env:USERPROFILE ".cloudflared"
$CertPem   = Join-Path $UserCfDir "cert.pem"
if (-not (Test-Path $CertPem)) {
    throw "No cert.pem at $CertPem. Run 'cloudflared tunnel login' first (browser auth for the substratesystems.io zone)."
}

# Service install + writing under System32 need a full admin token. Self-elevate so the
# behaviour is identical whether UAC is on (filtered token) or off (mirrors install-service.ps1).
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Host "Not elevated - relaunching as administrator (approve the UAC prompt)..."
    $hostExe = (Get-Process -Id $PID).Path; if (-not $hostExe) { $hostExe = "pwsh" }
    $relaunchArgs = @(
        "-NoExit", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "`"$PSCommandPath`"",
        "-Hostname", $Hostname, "-TunnelName", $TunnelName, "-Port", $Port, "-CloudflaredPath", "`"$CloudflaredExe`""
    )
    Start-Process -FilePath $hostExe -Verb RunAs -ArgumentList $relaunchArgs
    exit
}

# --- 1. Create the tunnel (or reuse an existing one), resolve its UUID ----------------
$existing = & $CloudflaredExe tunnel list --output json | ConvertFrom-Json
$tunnel = $existing | Where-Object { $_.name -eq $TunnelName } | Select-Object -First 1
if ($tunnel) {
    Write-Host "Tunnel '$TunnelName' already exists (id $($tunnel.id)) - reusing."
} else {
    Write-Host "Creating tunnel '$TunnelName'..."
    & $CloudflaredExe tunnel create $TunnelName | Write-Host
    $tunnel = (& $CloudflaredExe tunnel list --output json | ConvertFrom-Json) |
        Where-Object { $_.name -eq $TunnelName } | Select-Object -First 1
    if (-not $tunnel) { throw "Tunnel '$TunnelName' not found after create." }
}
$Uuid = $tunnel.id
$CredsSrc = Join-Path $UserCfDir "$Uuid.json"
if (-not (Test-Path $CredsSrc)) { throw "Tunnel credentials not found at $CredsSrc." }

# --- 2. Route the public hostname to this tunnel (idempotent) -------------------------
Write-Host "Routing DNS $Hostname -> tunnel $Uuid ..."
try { & $CloudflaredExe tunnel route dns $TunnelName $Hostname | Write-Host }
catch { Write-Warning "route dns returned: $_  (usually 'record already exists' - safe to ignore)" }

# --- 3. Stage config + creds where the SYSTEM service reads them ----------------------
$SysCfDir = "C:\Windows\System32\config\systemprofile\.cloudflared"
New-Item -ItemType Directory -Path $SysCfDir -Force | Out-Null
Copy-Item $CertPem  (Join-Path $SysCfDir "cert.pem")     -Force
Copy-Item $CredsSrc (Join-Path $SysCfDir "$Uuid.json")   -Force

$CredsDst = Join-Path $SysCfDir "$Uuid.json"
$ConfigPath = Join-Path $SysCfDir "config.yml"
@"
tunnel: $Uuid
credentials-file: $CredsDst
ingress:
  - hostname: $Hostname
    service: http://127.0.0.1:$Port
  - service: http_status:404
"@ | Set-Content -Path $ConfigPath -Encoding ascii
Write-Host "Wrote $ConfigPath"

# --- 4. Install (or refresh) + start the Windows service -----------------------------
$svc = Get-Service -Name "cloudflared" -ErrorAction SilentlyContinue
if ($svc) {
    Write-Host "cloudflared service exists - reinstalling to pick up config..."
    try { & $CloudflaredExe service uninstall | Write-Host } catch { Write-Warning "service uninstall: $_" }
    Start-Sleep -Seconds 2
}
Write-Host "Installing cloudflared service..."
& $CloudflaredExe service install | Write-Host
Set-Service -Name "cloudflared" -StartupType Automatic
Start-Service -Name "cloudflared"
Start-Sleep -Seconds 2

Write-Host ""
Write-Host "Tunnel '$TunnelName' ($Uuid) -> https://$Hostname -> http://127.0.0.1:$Port"
Write-Host "Service status: $((Get-Service cloudflared).Status)"
Write-Host ""
Write-Host "NEXT (not automated):"
Write-Host "  1. .env:  KB_MCP_BASE_URL=https://$Hostname"
Write-Host "  2. GitHub OAuth App: Homepage https://$Hostname ; callback https://$Hostname/auth/callback"
Write-Host "  3. Restart kb-mcp:  pwsh -File scripts/restart.ps1"
Write-Host "  4. claude.ai: re-add the connector at https://$Hostname/mcp (redo GitHub OAuth)"
Write-Host "  5. Cloudflare dashboard: for $Hostname turn OFF Bot Fight Mode + any WAF managed ruleset, Security Level low"
Write-Host "  6. Verify:  cloudflared tunnel info $TunnelName   and   curl.exe -i https://$Hostname/mcp  (expect 401)"
