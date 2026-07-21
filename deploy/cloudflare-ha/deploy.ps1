# Deploy the HA edge worker labeled with the current git SHA.
#
# A bare `wrangler deploy` still works, but /__version then reports
# "git_sha": "unlabeled" and doctor's ingress check surfaces that as a
# warning. This script exists so the deploy identity is not silently lost.
#
# Usage:
#   pwsh -File deploy/cloudflare-ha/deploy.ps1

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

function Fail($msg) {
    Write-Host "DEPLOY FAILED: $msg" -ForegroundColor Red
    exit 1
}

if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    Fail "git not found; install git to resolve the deploy SHA."
}
if (-not (Get-Command npx -ErrorAction SilentlyContinue)) {
    Fail "npx not found; install Node.js to run wrangler."
}

$gitSha = (git rev-parse --short HEAD 2>$null)
if ($LASTEXITCODE -ne 0 -or -not $gitSha) {
    Fail "git rev-parse --short HEAD failed; is this a git checkout?"
}

Write-Host "Deploying exomem-ha-edge at $gitSha ..." -ForegroundColor Cyan
& npx wrangler deploy --var "WORKER_GIT_SHA:$gitSha"
if ($LASTEXITCODE -ne 0) {
    Fail "wrangler deploy returned $LASTEXITCODE."
}
