# Grants the invoking user start/stop rights on the exomem service so future
# restarts don't require UAC. Idempotent — re-running with the ACE already
# present is a no-op.
#
# Usage:
#   pwsh -File scripts/grant-no-uac.ps1
#   (script self-elevates for the actual sdset)

$ErrorActionPreference = "Stop"
$ServiceName = "exomem"

$account = "$env:USERDOMAIN\$env:USERNAME"
$sid = (New-Object System.Security.Principal.NTAccount($account)).Translate([System.Security.Principal.SecurityIdentifier]).Value
Write-Host "User:        $account"
Write-Host "SID:         $sid"

$currentAcl = (sc.exe sdshow $ServiceName | Where-Object { $_ -match '^D:' } | Select-Object -First 1).Trim()
Write-Host "Current ACL: $currentAcl"

if (-not $currentAcl) {
    throw "Could not read current service ACL. Is $ServiceName installed?"
}

if ($currentAcl -match [Regex]::Escape($sid)) {
    Write-Host ""
    Write-Host "ACE for $sid already present. No change needed." -ForegroundColor Green
    exit 0
}

$newAcl = $currentAcl + "(A;;RPWPCR;;;$sid)"
Write-Host "New ACL:     $newAcl"
Write-Host ""
Write-Host "About to set the new ACL (requires UAC)..."

Start-Process -Verb RunAs -Wait sc.exe -ArgumentList @("sdset", $ServiceName, $newAcl)

# Verify it took.
$verifyAcl = (sc.exe sdshow $ServiceName | Where-Object { $_ -match '^D:' } | Select-Object -First 1).Trim()
if ($verifyAcl -match [Regex]::Escape($sid)) {
    Write-Host ""
    Write-Host "Success. $account can now stop/start $ServiceName without UAC." -ForegroundColor Green
    Write-Host "  sc.exe stop $ServiceName"
    Write-Host "  sc.exe start $ServiceName"
} else {
    Write-Warning "sdset did not stick. Final ACL: $verifyAcl"
}
