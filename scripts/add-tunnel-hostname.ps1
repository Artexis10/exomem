# Add an extra public hostname to an EXISTING Cloudflare Tunnel, non-destructively.
#
# setup-cloudflared.ps1 provisions ONE hostname and reinstalls the service. This
# script instead ADDS a hostname to a tunnel that is already serving: it inserts
# one ingress rule (pointing at the same local service) ahead of the catch-all,
# leaving every existing hostname untouched, and bounces only the cloudflared
# service. Use it to alias a second name (e.g. exomem.example.com alongside an
# existing kb.example.com) onto the same backend.
#
# Prereq: the DNS route must already exist:
#     cloudflared tunnel route dns <TunnelName> <Hostname>
# Run from an ELEVATED PowerShell (the config lives under System32).
#
# Usage:
#   pwsh -File scripts/add-tunnel-hostname.ps1 -Hostname exomem.substratesystems.io
#   pwsh -File scripts/add-tunnel-hostname.ps1 -Hostname exomem.example.com -Port 8765

param(
    [Parameter(Mandatory = $true)][string]$Hostname,
    [int]$Port = 8765,
    [string]$ConfigPath = "C:\Windows\System32\config\systemprofile\.cloudflared\config.yml",
    [string]$ServiceName = "cloudflared"
)

$ErrorActionPreference = "Stop"

# --- Must be elevated: the config lives under System32 ------------------------
$isAdmin = ([Security.Principal.WindowsPrincipal] `
    [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    throw "Run this from an ELEVATED PowerShell: writing $ConfigPath needs admin."
}
if (-not (Test-Path $ConfigPath)) { throw "No tunnel config at $ConfigPath." }

$lines = Get-Content -LiteralPath $ConfigPath
if ($lines -match [regex]::Escape("hostname: $Hostname")) {
    Write-Host "Hostname '$Hostname' is already in the ingress; nothing to do."
    exit 0
}

# --- Back up before touching the live config ---------------------------------
$backup = "$ConfigPath.bak-$(Get-Date -Format yyyyMMdd-HHmmss)"
Copy-Item -LiteralPath $ConfigPath -Destination $backup -Force
Write-Host "Backed up config -> $backup"

# --- Insert the new rule immediately before the catch-all (http_status:404) --
# Preserving all existing rules; the catch-all MUST stay last.
$catchAll = ($lines | Select-String -SimpleMatch "service: http_status:404" | Select-Object -First 1)
if (-not $catchAll) { throw "No catch-all 'service: http_status:404' rule found; refusing to guess ingress order." }
$idx = $catchAll.LineNumber - 1  # Select-String is 1-based
$insert = @(
    "  - hostname: $Hostname",
    "    service: http://127.0.0.1:$Port"
)
$updated = @()
$updated += $lines[0..($idx - 1)]
$updated += $insert
$updated += $lines[$idx..($lines.Count - 1)]
Set-Content -LiteralPath $ConfigPath -Value $updated -Encoding ascii
Write-Host "Inserted ingress rule for $Hostname -> http://127.0.0.1:$Port"

# --- Restart only the tunnel service (surgical; no reinstall) ----------------
Write-Host "Restarting $ServiceName ..."
Restart-Service -Name $ServiceName -Force
Start-Sleep -Seconds 3

# --- Verify: the new hostname should reach the service (401 = healthy MCP) ---
try {
    $r = Invoke-WebRequest -Uri "https://$Hostname/mcp" -Method GET -SkipHttpErrorCheck -TimeoutSec 15
    Write-Host "https://$Hostname/mcp -> HTTP $($r.StatusCode) (401 = healthy auth funnel)"
} catch {
    Write-Warning "Verification request failed: $_  (DNS may still be propagating; retry in a minute)"
}
Write-Host "Done. Existing hostnames unchanged; '$Hostname' now routes to the same service."
