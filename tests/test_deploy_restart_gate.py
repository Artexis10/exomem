"""A deploy must not report success while the old interpreter is still serving.

`scripts/deploy.ps1` step 5 is titled "Verify the RUNNING process, not the
installer" and polls `/health`. But `/health` reports
`importlib.metadata.version("exomem")`, read from disk at request time with no
relation to the code the running interpreter loaded. Observed live: `uv pip
install` swapped the wheel under a process that had been up since 12:20:10Z;
`/health` answered `0.53.0` while the interpreter still ran 0.52.3, and in fact
ran a *mixed* build, because modules imported lazily after the swap came from
the new wheel. Every latency measurement taken that day was attributed to a
release that was never running.

The same blindness is structural, not incidental: `install-info --json` and
`/health` both read the same distribution metadata, so any gate comparing one
against the other compares disk with disk and cannot fail for this reason. The
worker process identity is the only observable that separates "restarted onto
the new code" from "still running the old code".

Two gates are exercised here, both as pure before/after assertions so they run
everywhere pwsh does rather than needing a real Windows service:

1. `Assert-ExomemServiceRestarted` — the restart gate this adds.
2. `Test-ExomemAcceleratedTorch` — the accelerator gate, which read
   `.accelerated` off `install-info --json`. That key has never been emitted, so
   the property was always $null, `[bool]$null` is $false, and the guard
   documented as "a hard failure by default" could not fire at all.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import stat
import subprocess
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
COMMON = ROOT / "scripts" / "_service-common.ps1"
RESTART = ROOT / "scripts" / "restart.ps1"
DEPLOY = ROOT / "scripts" / "deploy.ps1"
UPGRADE = ROOT / "scripts" / "upgrade.ps1"
INSTALL_SERVICE = ROOT / "scripts" / "install-service.ps1"
DEPLOYMENT_DOC = ROOT / "docs" / "deployment.md"
HOSTED_DEPLOY_DOC = ROOT / "docs" / "runbooks" / "hosted" / "deploy.md"
HOSTED_UPGRADE_DOC = ROOT / "docs" / "runbooks" / "hosted" / "runtime-upgrades.md"
HOSTED_INIT_JOB = ROOT / "infra" / "helm" / "cell" / "templates" / "init-job.yaml"
COMMON_SH = ROOT / "scripts" / "_service-common.sh"
UPGRADE_SH = ROOT / "scripts" / "upgrade.sh"
INSTALL_SERVICE_SH = ROOT / "scripts" / "install-service.sh"
TRANSITION_TOOL = ROOT / "scripts" / "service-transition-receipt.py"
PWSH = shutil.which("pwsh")

pytestmark = pytest.mark.skipif(PWSH is None, reason="pwsh is not available")

_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

_RESTART_DRIVER = """
    param([string]$Common, [int]$Before, [int]$After)
    $ErrorActionPreference = "Stop"
    try {
        . $Common
        Assert-ExomemServiceRestarted -Before $Before -After $After -ServiceName "exomem"
    } catch {
        Write-Host "DRIVER-ERROR: $($_.Exception.Message)"
        exit 1
    }
    Write-Host "DRIVER-COMPLETED"
"""

_STOPPED_DRIVER = """
    param([string]$Common, [int]$Before, [int]$After = 0, [int]$AlivePid = 0)
    $ErrorActionPreference = "Stop"
    . $Common
    function Get-Service {
        [CmdletBinding()]
        param([string]$Name)
        return [pscustomobject]@{ Status = "Stopped" }
    }
    function Get-Process {
        [CmdletBinding()]
        param([int]$Id)
        if ($AlivePid -and $Id -eq $AlivePid) { return [pscustomobject]@{ Id = $Id } }
        return $null
    }
    try {
        Assert-ExomemServiceStopped -Before $Before -After $After -ServiceName "exomem"
    } catch {
        Write-Host "DRIVER-ERROR: $($_.Exception.Message)"
        exit 1
    }
    Write-Host "DRIVER-COMPLETED"
"""

_WORKER_FALLBACK_DRIVER = """
    param([string]$Common)
    $ErrorActionPreference = "Stop"
    . $Common
    $env:OS = "Windows_NT"
    function Get-CimInstance { throw "injected CIM access denied" }
    function Get-ExomemServiceEndpoint { return @{ Host = "127.0.0.1"; Port = 8765 } }
    function netstat {
        return @(
            "  TCP    127.0.0.1:8765    0.0.0.0:0    LISTENING    4312",
            "  TCP    127.0.0.1:54000   127.0.0.1:8765 ESTABLISHED  9001"
        )
    }
    $worker = Get-ExomemServiceWorkerPid -ServiceName "exomem"
    Write-Host "WORKER=$worker"
    if ($worker -ne 4312) { exit 1 }
"""

_STATE_BINDING_DRIVER = """
    param([string]$Common, [string]$AppDirectory, [string]$OperatorLocal, [string]$SystemLocal)
    $ErrorActionPreference = "Stop"
    . $Common
    $env:LOCALAPPDATA = $OperatorLocal
    $operator = Ensure-ExomemDotenvStateRootBinding -AppDirectory $AppDirectory
    $env:LOCALAPPDATA = $SystemLocal
    $service = Ensure-ExomemDotenvStateRootBinding -AppDirectory $AppDirectory
    if ($operator -ne $service) { exit 1 }
    Write-Host "BOUND=$service"
"""

_STOPPED_RESUME_DRIVER = """
    param([string]$Common, [int]$ListenerPid = 0, [int]$AlivePid = 0)
    $ErrorActionPreference = "Stop"
    . $Common
    function Get-Service {
        [CmdletBinding()]
        param([string]$Name)
        return [pscustomobject]@{ Status = "Stopped" }
    }
    function Get-ExomemListenerPidsForPort {
        if ($ListenerPid) { return @($ListenerPid) }
        return @()
    }
    function Get-Process {
        [CmdletBinding()]
        param([int]$Id)
        if ($AlivePid -and $Id -eq $AlivePid) { return [pscustomobject]@{ Id = $Id } }
        return $null
    }
    try {
        $receipt = [pscustomobject]@{
            service_id = "exomem"
            phase = "failed"
            proof_pids = @($AlivePid) | Where-Object { $_ }
            port = 8765
            target_port = 8765
        }
        Assert-ExomemStoppedResumeAuthority -ServiceName "exomem" -Receipt $receipt
    } catch {
        Write-Host "DRIVER-ERROR: $($_.Exception.Message)"
        exit 1
    }
    Write-Host "DRIVER-COMPLETED"
"""

_RECEIPT_RESUME_DRIVER = """
    param(
        [string]$Common,
        [string]$Python,
        [string]$ReceiptRoot,
        [string]$BindingPath,
        [string]$StateRoot,
        [string]$Vault,
        [ValidateSet("create", "resume")][string]$Mode,
        [int]$CapturedPid = 0,
        [int]$TargetPort = 8765
    )
    $ErrorActionPreference = "Stop"
    $env:EXOMEM_TRANSITION_RECEIPT_ROOT = $ReceiptRoot
    . $Common
    function Get-Service {
        [CmdletBinding()]
        param([string]$Name)
        return [pscustomobject]@{ Status = "Stopped" }
    }
    function Get-ExomemConfiguredListenerPids { return @() }
    function Get-ExomemListenerPidsForPort { return @() }
    try {
        if ($Mode -eq "create") {
            New-ExomemTransitionReceipt `
                -PythonPath $Python `
                -ServiceName "exomem" `
                -BindingPath $BindingPath `
                -StateRoot $StateRoot `
                -VaultPath $Vault `
                -Port $TargetPort `
                -TargetPort $TargetPort `
                -WorkerPid $CapturedPid `
                -ListenerPids @($CapturedPid) | Out-Null
        } else {
            $receipt = Read-ExomemTransitionReceipt `
                -PythonPath $Python `
                -ServiceName "exomem" `
                -BindingPath $BindingPath `
                -StateRoot $StateRoot `
                -VaultPath $Vault `
                -TargetPort $TargetPort
            Assert-ExomemStoppedResumeAuthority `
                -ServiceName "exomem" `
                -Receipt $receipt
        }
    } catch {
        Write-Host "DRIVER-ERROR: $($_.Exception.Message)"
        exit 1
    }
    Write-Host "DRIVER-COMPLETED"
"""

_FAILED_START_RECEIPT_DRIVER = """
    param(
        [string]$Common,
        [string]$Python,
        [string]$ReceiptRoot,
        [string]$BindingPath,
        [string]$StateRoot,
        [string]$Vault,
        [int]$OriginalPort,
        [int]$TargetPort,
        [int]$TargetWorkerPid,
        [int]$TargetListenerPid,
        [ValidateSet(
            "publish",
            "enumeration-unavailable",
            "listener-hidden",
            "listener-ambiguous",
            "receipt-write-failure",
            "failed-phase-write-failure"
        )]
        [string]$Mode = "publish"
    )
    $ErrorActionPreference = "Stop"
    $env:EXOMEM_TRANSITION_RECEIPT_ROOT = $ReceiptRoot
    . $Common
    if ($Mode -eq "publish") {
        function Get-ExomemListenerPidsForPort {
            param([int]$Port)
            if ($Port -eq $TargetPort) { return @($TargetListenerPid) }
            return @()
        }
    } elseif ($Mode -eq "enumeration-unavailable") {
        function Get-ExomemListenerPidsForPort {
            throw "injected listener enumeration unavailable"
        }
    } elseif ($Mode -eq "listener-hidden") {
        function netstat {
            return "  TCP    127.0.0.1:$TargetPort    0.0.0.0:0    LISTENING    -"
        }
    } elseif ($Mode -eq "listener-ambiguous") {
        function netstat {
            return @(
                "  TCP    127.0.0.1:$TargetPort    0.0.0.0:0    LISTENING    4312",
                "  TCP    0.0.0.0:$TargetPort      0.0.0.0:0    LISTENING    hidden"
            )
        }
    } elseif ($Mode -eq "receipt-write-failure") {
        function Set-ExomemTransitionReceiptPhase {
            throw "injected receipt write failure"
        }
    } elseif ($Mode -eq "failed-phase-write-failure") {
        $script:OriginalSetTransitionReceiptPhase = ${function:Set-ExomemTransitionReceiptPhase}
        $script:ReceiptWriteCount = 0
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
            $script:ReceiptWriteCount += 1
            if ($script:ReceiptWriteCount -eq 2) {
                throw "injected failed-phase write failure"
            }
            & $script:OriginalSetTransitionReceiptPhase @PSBoundParameters
        }
    }
    try {
        $receipt = Publish-ExomemFailedTransitionReceipt `
            -PythonPath $Python `
            -ServiceName "exomem" `
            -BindingPath $BindingPath `
            -StateRoot $StateRoot `
            -VaultPath $Vault `
            -TargetPort $TargetPort `
            -ObservedWorkerPid $TargetWorkerPid
        Write-Host "PHASE=$($receipt.phase)"
        Write-Host "PROOF=$(@($receipt.proof_pids) -join ',')"
    } catch {
        Write-Host "DRIVER-ERROR: $($_.Exception.Message)"
        exit 1
    }
    Write-Host "DRIVER-COMPLETED"
"""

_LIVE_ACCEPTANCE_DRIVER = """
    param(
        [string]$Common,
        [int]$WorkerPid,
        [int]$ListenerPid,
        [switch]$ListenerIsDescendant,
        [int]$CurrentWorkerPid = 0,
        [switch]$ListenerChangesAfterProof,
        [string]$ExpectedVersion,
        [string]$ServedVersion
    )
    $ErrorActionPreference = "Stop"
    . $Common
    if (-not $CurrentWorkerPid) { $CurrentWorkerPid = $WorkerPid }
    $script:ListenerReads = 0
    function Get-ExomemConfiguredListenerPids {
        $script:ListenerReads += 1
        if ($ListenerChangesAfterProof -and $script:ListenerReads -gt 1) {
            return @($ListenerPid + 1)
        }
        return @($ListenerPid)
    }
    function Get-ExomemServiceWorkerPid { return $CurrentWorkerPid }
    function Test-ExomemProcessTreeMembership {
        param([int]$RootPid, [int]$CandidatePid)
        return $RootPid -eq $CandidatePid -or [bool]$ListenerIsDescendant
    }
    function Invoke-RestMethod {
        return [pscustomobject]@{ version = $ServedVersion }
    }
    try {
        Assert-ExomemListenerOwnedByWorker -ServiceName "exomem" -WorkerPid $WorkerPid
        Wait-ExomemHealthVersion `
            -HealthUrl "http://127.0.0.1:8765/health" `
            -ExpectedVersion $ExpectedVersion `
            -TimeoutSec 1 | Out-Null
    } catch {
        Write-Host "DRIVER-ERROR: $($_.Exception.Message)"
        exit 1
    }
    Write-Host "DRIVER-COMPLETED"
"""

_PROCESS_TREE_DRIVER = """
    param(
        [string]$Common,
        [int]$RootPid,
        [int]$CandidatePid,
        [string]$ParentMap = "",
        [switch]$RootIsNewer
    )
    $ErrorActionPreference = "Stop"
    . $Common
    $script:Parents = @{}
    foreach ($entry in @($ParentMap -split ",") | Where-Object { $_ }) {
        $pair = $entry -split ":", 2
        $script:Parents[[int]$pair[0]] = [int]$pair[1]
    }
    function Get-CimInstance {
        param([string]$ClassName, [string]$Filter, [string]$ErrorAction)
        if ($ClassName -ne "Win32_Process" -or $Filter -notmatch '^ProcessId=(\\d+)$') {
            throw "unexpected process query"
        }
        $processId = [int]$Matches[1]
        if (-not $script:Parents.ContainsKey($processId)) { return $null }
        $created = ([datetime]"2026-01-01T00:00:00Z").AddSeconds($processId)
        if ($RootIsNewer -and $processId -eq $RootPid) {
            $created = [datetime]"2030-01-01T00:00:00Z"
        }
        return [pscustomobject]@{
            ProcessId = $processId
            ParentProcessId = [int]$script:Parents[$processId]
            CreationDate = $created
        }
    }
    if (Test-ExomemProcessTreeMembership -RootPid $RootPid -CandidatePid $CandidatePid) {
        Write-Host "MEMBER"
        exit 0
    }
    Write-Host "NOT-MEMBER"
    exit 1
"""

_INSTALL_MCP_FAILURE_CLEANUP_DRIVER = r"""
    param(
        [string]$InstallScript,
        [string]$Common,
        [string]$Python,
        [string]$ReceiptRoot,
        [string]$BindingPath,
        [string]$StateRoot,
        [string]$Vault,
        [int]$TargetPort,
        [int]$AcceptedWorkerPid,
        [int]$ReplacementPid,
        [string]$StopSignal,
        [string]$ClosedSignal
    )
    $ErrorActionPreference = "Stop"
    $env:EXOMEM_TRANSITION_RECEIPT_ROOT = $ReceiptRoot
    . $Common

    $tokens = $null
    $parseErrors = $null
    $ast = [System.Management.Automation.Language.Parser]::ParseFile(
        $InstallScript,
        [ref]$tokens,
        [ref]$parseErrors
    )
    if ($parseErrors.Count) { throw "installer parse failed" }
    $functionNames = @("Wait-ServiceState", "Stop-FailedStateRootTransition")
    $functionAsts = @($ast.FindAll({
        param($node)
        $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
            $functionNames -contains $node.Name
    }, $true))
    if ($functionAsts.Count -ne 2) { throw "installer cleanup functions not found" }
    foreach ($functionAst in $functionAsts) {
        Invoke-Expression $functionAst.Extent.Text
    }
    $mcpTryAsts = @($ast.FindAll({
        param($node)
        $node -is [System.Management.Automation.Language.TryStatementAst] -and
            $node.Body.Extent.Text -match 'Test-McpEndpoint\s+-HostName'
    }, $true))
    if ($mcpTryAsts.Count -ne 1) { throw "installer MCP verification catch not found" }
    $mcpTryText = $mcpTryAsts[0].Extent.Text

    $script:FakeStopped = $false
    function Get-Service {
        [CmdletBinding()]
        param([string]$Name)
        $status = if ($script:FakeStopped) { "Stopped" } else { "Running" }
        return [pscustomobject]@{ Status = $status }
    }
    function Get-Process {
        [CmdletBinding()]
        param([int]$Id)
        if ($Id -eq $ReplacementPid) { return [pscustomobject]@{ Id = $Id } }
        return $null
    }
    function Get-ExomemServiceWorkerPid {
        param([string]$ServiceName)
        if ($script:FakeStopped) { return $AcceptedWorkerPid }
        return $ReplacementPid
    }
    function Get-ExomemListenerPidsForPort {
        param([int]$Port)
        if (-not $script:FakeStopped -and $Port -eq $TargetPort) {
            return @($ReplacementPid)
        }
        return @()
    }
    function Test-McpEndpoint {
        param([string]$HostName, [int]$EndpointPort)
        throw "injected MCP verification failure"
    }
    function Invoke-FakeNssm {
        if ($args.Count -and $args[0] -eq "stop") {
            Set-Content -LiteralPath $StopSignal -Value "stop"
            $deadline = (Get-Date).AddSeconds(10)
            while (-not (Test-Path -LiteralPath $ClosedSignal) -and (Get-Date) -lt $deadline) {
                Start-Sleep -Milliseconds 20
            }
            if (-not (Test-Path -LiteralPath $ClosedSignal)) {
                throw "replacement listener did not close"
            }
            $script:FakeStopped = $true
        }
    }

    $script:StateRootTransitionBegan = $true
    $ServiceName = "exomem"
    $BindHost = "127.0.0.1"
    $Port = $TargetPort
    $NssmPath = "Invoke-FakeNssm"
    $workerBefore = 2147483001
    $workerAfter = $AcceptedWorkerPid
    $existingTransition = $true
    $transitionIdentity = @{
        PythonPath = $Python
        ServiceName = $ServiceName
        BindingPath = $BindingPath
        StateRoot = $StateRoot
        VaultPath = $Vault
        TargetPort = $TargetPort
    }

    try {
        Invoke-Expression $mcpTryText
    } catch {
        Write-Host "MCP-ERROR=$($_.Exception.Message)"
        Stop-FailedStateRootTransition
    }

    $receipt = Read-ExomemTransitionReceipt @transitionIdentity
    Write-Host "PHASE=$($receipt.phase)"
    Write-Host "PROOF=$(@($receipt.proof_pids) -join ',')"
    try {
        Assert-ExomemStoppedResumeAuthority -ServiceName $ServiceName -Receipt $receipt
        Write-Host "RESUME_AUTHORIZED"
    } catch {
        Write-Host "RESUME_REFUSED=$($_.Exception.Message)"
    }
"""

_FRESH_INSTALL_DOCTOR_FAILURE_DRIVER = r"""
    param(
        [string]$InstallScript,
        [string]$Common,
        [string]$Python,
        [string]$TestRoot
    )
    $ErrorActionPreference = "Stop"
    $env:EXOMEM_TRANSITION_RECEIPT_ROOT = Join-Path $TestRoot "receipts"
    $env:ProgramData = Join-Path $TestRoot "program-data"
    . $Common

    $tokens = $null
    $parseErrors = $null
    $ast = [System.Management.Automation.Language.Parser]::ParseFile(
        $InstallScript,
        [ref]$tokens,
        [ref]$parseErrors
    )
    if ($parseErrors.Count) { throw "installer parse failed" }
    foreach ($functionAst in @($ast.FindAll({
        param($node)
        $node -is [System.Management.Automation.Language.FunctionDefinitionAst]
    }, $true))) {
        Invoke-Expression $functionAst.Extent.Text
    }
    $statements = @($ast.EndBlock.Statements)
    $mainStart = -1
    $mainEnd = -1
    for ($index = 0; $index -lt $statements.Count; $index++) {
        $text = $statements[$index].Extent.Text.Trim()
        if ($mainStart -lt 0 -and $text.StartsWith('$existingService =')) {
            $mainStart = $index
        } elseif (
            $mainStart -ge 0 -and
            $text -eq '$script:StateRootTransitionBegan = $false'
        ) {
            $mainEnd = $index
            break
        }
    }
    if ($mainStart -lt 0 -or $mainEnd -lt $mainStart) {
        throw "installer main transition not found"
    }
    $mainText = ($statements[$mainStart..$mainEnd].Extent.Text -join "`n")

    $repoRoot = Join-Path $TestRoot "repo"
    $logDir = Join-Path $repoRoot "logs"
    $vault = Join-Path $TestRoot "vault"
    $stateRoot = Join-Path $TestRoot "state"
    New-Item -ItemType Directory -Path $repoRoot, $logDir, $vault -Force | Out-Null
    Set-Content -LiteralPath (Join-Path $repoRoot ".env") -Value @(
        "EXOMEM_VAULT_PATH=$vault",
        "EXOMEM_STATE_ROOT=$stateRoot"
    )

    $NssmPath = "Invoke-FakeNssm"
    $ServiceName = "exomem"
    $BindHost = "127.0.0.1"
    $Port = 8765
    $Profile = "lean"
    $Release = $true
    $ServiceRoot = ""
    $PackageVersion = ""
    $CudaTorch = "never"
    $LegacyMcpCompat = $false
    $script:Registered = $false
    $script:ServiceStatus = "Stopped"
    $script:AutoStart = $false
    $script:FailRemoteDoctor = $true
    $script:TargetWorkerPid = 4312

    function Get-Service {
        [CmdletBinding()]
        param([string]$Name)
        if (-not $script:Registered) { return $null }
        return [pscustomobject]@{ Status = $script:ServiceStatus }
    }
    function Get-ExomemServiceAppDirectory {
        param([string]$ServiceName)
        return $repoRoot
    }
    function Get-ExomemServicePython {
        param([string]$ServiceName)
        return $Python
    }
    function Get-ExomemServiceEndpoint {
        param([string]$ServiceName)
        return @{ Host = "127.0.0.1"; Port = $Port }
    }
    function Get-ExomemServiceWorkerPid {
        param([string]$ServiceName)
        if ($script:Registered -and $script:ServiceStatus -eq "Running") {
            return $script:TargetWorkerPid
        }
        return 0
    }
    function Get-ExomemConfiguredListenerPids {
        param([string]$ServiceName)
        if ($script:Registered -and $script:ServiceStatus -eq "Running") {
            return @($script:TargetWorkerPid)
        }
        return @()
    }
    function Get-ExomemListenerPidsForPort {
        param([int]$Port)
        if ($script:Registered -and $script:ServiceStatus -eq "Running") {
            return @($script:TargetWorkerPid)
        }
        return @()
    }
    function Install-ReleaseVenv { return "Invoke-FakeTargetPython" }
    function Get-ExomemInstalledVersion {
        param([string]$PythonPath)
        return "1.2.3"
    }
    function Invoke-ExomemNative {
        param([string[]]$CommandArgs)
        return [pscustomobject]@{ ExitCode = 0; Output = "{}" }
    }
    function Invoke-FakeTargetPython {
        $joined = $args -join " "
        if ($joined -match 'doctor' -and $joined -match '--profile remote') {
            if ($script:FailRemoteDoctor) {
                $global:LASTEXITCODE = 1
                return
            }
        }
        $global:LASTEXITCODE = 0
    }
    function Invoke-FakeNssm {
        $global:LASTEXITCODE = 0
        if (-not $args.Count) { return }
        switch ($args[0]) {
            "install" {
                $script:Registered = $true
                $script:ServiceStatus = "Stopped"
            }
            "set" {
                if ($args.Count -ge 4 -and $args[2] -eq "Start") {
                    $script:AutoStart = $args[3] -eq "SERVICE_AUTO_START"
                }
            }
            "stop" {
                if ($script:Registered) { $script:ServiceStatus = "Stopped" }
            }
            "start" {
                if (-not $script:Registered) { throw "service is not registered" }
                $script:ServiceStatus = "Running"
            }
        }
    }
    function Wait-ExomemHealthVersion {
        param([string]$HealthUrl, [string]$ExpectedVersion, [int]$TimeoutSec)
        return [pscustomobject]@{ version = $ExpectedVersion }
    }
    function Test-McpEndpoint {
        param([string]$HostName, [int]$EndpointPort)
    }

    function Invoke-InstallerMain {
        param([bool]$Resume, [bool]$FailRemoteDoctor)
        $ResumeStoppedTransition = $Resume
        $script:FailRemoteDoctor = $FailRemoteDoctor
        $script:StateRootTransitionBegan = $false
        $workerBefore = 0
        $workerAfter = 0
        $transitionReceipt = $null
        $transitionIdentity = $null
        $existingTransition = $false
        try {
            Invoke-Expression $mainText
            return "completed"
        } catch {
            $failure = $_.Exception.Message
            Stop-FailedStateRootTransition
            return $failure
        }
    }

    $first = Invoke-InstallerMain -Resume $false -FailRemoteDoctor $true
    $firstRegistered = $script:Registered
    $firstAutoStart = $script:AutoStart
    $receiptPath = Join-Path $env:EXOMEM_TRANSITION_RECEIPT_ROOT "exomem.json"
    Write-Host "FIRST_ERROR=$first"
    Write-Host "FIRST_REGISTERED=$firstRegistered"
    Write-Host "FIRST_AUTOSTART=$firstAutoStart"
    Write-Host "FIRST_RECEIPT=$(Test-Path -LiteralPath $receiptPath)"

    if ($firstRegistered) {
        $normalRetry = Invoke-InstallerMain -Resume $false -FailRemoteDoctor $false
        Write-Host "NORMAL_RETRY=$normalRetry"
        $resumeRetry = Invoke-InstallerMain -Resume $true -FailRemoteDoctor $false
        Write-Host "RESUME_RETRY=$resumeRetry"
    } else {
        $normalRetry = Invoke-InstallerMain -Resume $false -FailRemoteDoctor $false
        Write-Host "NORMAL_RETRY=$normalRetry"
        Write-Host "NORMAL_RETRY_STATUS=$($script:ServiceStatus)"
    }
"""

_TORCH_DRIVER = """
    param([string]$Common, [string]$Python)
    $ErrorActionPreference = "Stop"
    . $Common
    $version = Get-ExomemTorchVersion -PythonPath $Python
    $accel = Test-ExomemAcceleratedTorch -PythonPath $Python
    Write-Host "VERSION=$version"
    Write-Host "ACCEL=$accel"
"""

# Stands in for the service venv's interpreter: the probes run
# `<python> -c "import importlib.metadata ..."` and read one line of stdout.
_PYTHON_SH = """
    #!/bin/sh
    if [ -n "$FAKE_TORCH_VERSION" ]; then echo "$FAKE_TORCH_VERSION"; exit 0; fi
    exit 1
"""

_PYTHON_CMD = """
    @echo off
    if not defined FAKE_TORCH_VERSION exit /b 1
    echo %FAKE_TORCH_VERSION%
    exit /b 0
"""


def _write_executable(path: Path, body: str) -> None:
    path.write_text(textwrap.dedent(body).lstrip(), encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _flat(result: subprocess.CompletedProcess[str]) -> str:
    """Both streams, ANSI stripped and whitespace collapsed.

    PowerShell wraps what it renders itself to the host width, so a phrase can be
    split mid-sentence by a newline that depends on the terminal running it.
    """
    return " ".join(_ANSI.sub("", result.stdout + result.stderr).split())


def _run(script: Path, *args: str, env: dict[str, str] | None = None):
    assert PWSH is not None
    return subprocess.run(
        [PWSH, "-NoProfile", "-File", str(script), *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=180,
    )


@pytest.fixture()
def restart_driver(tmp_path: Path) -> Path:
    script = tmp_path / "restart-driver.ps1"
    _write_executable(script, _RESTART_DRIVER)
    return script


@pytest.fixture()
def stopped_driver(tmp_path: Path) -> Path:
    script = tmp_path / "stopped-driver.ps1"
    _write_executable(script, _STOPPED_DRIVER)
    return script


@pytest.fixture()
def worker_fallback_driver(tmp_path: Path) -> Path:
    script = tmp_path / "worker-fallback-driver.ps1"
    _write_executable(script, _WORKER_FALLBACK_DRIVER)
    return script


@pytest.fixture()
def state_binding_driver(tmp_path: Path) -> Path:
    script = tmp_path / "state-binding-driver.ps1"
    _write_executable(script, _STATE_BINDING_DRIVER)
    return script


@pytest.fixture()
def stopped_resume_driver(tmp_path: Path) -> Path:
    script = tmp_path / "stopped-resume-driver.ps1"
    _write_executable(script, _STOPPED_RESUME_DRIVER)
    return script


@pytest.fixture()
def receipt_resume_driver(tmp_path: Path) -> Path:
    script = tmp_path / "receipt-resume-driver.ps1"
    _write_executable(script, _RECEIPT_RESUME_DRIVER)
    return script


@pytest.fixture()
def failed_start_receipt_driver(tmp_path: Path) -> Path:
    script = tmp_path / "failed-start-receipt-driver.ps1"
    _write_executable(script, _FAILED_START_RECEIPT_DRIVER)
    return script


@pytest.fixture()
def live_acceptance_driver(tmp_path: Path) -> Path:
    script = tmp_path / "live-acceptance-driver.ps1"
    _write_executable(script, _LIVE_ACCEPTANCE_DRIVER)
    return script


@pytest.fixture()
def process_tree_driver(tmp_path: Path) -> Path:
    script = tmp_path / "process-tree-driver.ps1"
    _write_executable(script, _PROCESS_TREE_DRIVER)
    return script


def _create_starting_receipt(
    *,
    receipt_root: Path,
    binding: Path,
    state_root: Path,
    vault: Path,
    original_port: int,
    target_port: int,
    phase: str = "starting",
) -> Path:
    receipt = receipt_root / "exomem.json"
    identity = (
        "--path",
        str(receipt),
        "--service-id",
        "exomem",
        "--binding-path",
        str(binding),
        "--state-root",
        str(state_root),
        "--vault",
        str(vault),
        "--target-port",
        str(target_port),
    )
    created = subprocess.run(
        [
            os.sys.executable,
            str(TRANSITION_TOOL),
            "create",
            *identity,
            "--port",
            str(original_port),
            "--worker-pid",
            "2147483000",
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert created.returncode == 0, created.stderr
    transitioned = subprocess.run(
        [
            os.sys.executable,
            str(TRANSITION_TOOL),
            "phase",
            *identity,
            "--phase",
            phase,
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert transitioned.returncode == 0, transitioned.stderr
    return receipt


class TestRestartGate:
    def test_an_unchanged_worker_pid_fails_the_deploy(self, restart_driver: Path) -> None:
        """The live defect: wheel replaced, process never reloaded."""
        result = _run(restart_driver, "-Common", str(COMMON), "-Before", "27372", "-After", "27372")
        flat = _flat(result)
        assert result.returncode == 1, flat
        assert "still running the same worker process" in flat, flat
        assert "27372" in flat, flat

    def test_a_changed_worker_pid_is_accepted(self, restart_driver: Path) -> None:
        result = _run(restart_driver, "-Common", str(COMMON), "-Before", "27372", "-After", "31005")
        flat = _flat(result)
        assert result.returncode == 0, flat
        assert "DRIVER-COMPLETED" in flat, flat
        assert "27372 -> 31005" in flat, flat

    def test_no_worker_after_restart_is_fatal(self, restart_driver: Path) -> None:
        """A service that came back with nothing running is not a success."""
        result = _run(restart_driver, "-Common", str(COMMON), "-Before", "27372", "-After", "0")
        flat = _flat(result)
        assert result.returncode == 1, flat
        assert "no running worker process" in flat, flat

    def test_an_unknown_baseline_warns_instead_of_stranding_the_box(
        self, restart_driver: Path
    ) -> None:
        """Refusing every deploy where the probe is unavailable is worse."""
        result = _run(restart_driver, "-Common", str(COMMON), "-Before", "0", "-After", "31005")
        flat = _flat(result)
        assert result.returncode == 0, flat
        assert "DRIVER-COMPLETED" in flat, flat
        assert "No pre-restart worker pid" in flat, flat


class TestStoppedGate:
    def test_orphaned_captured_worker_refuses_offline_migration(
        self, stopped_driver: Path
    ) -> None:
        result = _run(
            stopped_driver,
            "-Common",
            str(COMMON),
            "-Before",
            "27372",
            "-AlivePid",
            "27372",
        )
        flat = _flat(result)
        assert result.returncode == 1, flat
        assert "captured worker pid 27372 is still alive" in flat, flat

    def test_stopped_scm_and_absent_captured_worker_proves_offline_window(
        self, stopped_driver: Path
    ) -> None:
        result = _run(
            stopped_driver,
            "-Common",
            str(COMMON),
            "-Before",
            "27372",
        )
        flat = _flat(result)
        assert result.returncode == 0, flat
        assert "DRIVER-COMPLETED" in flat, flat

    def test_post_start_orphan_is_also_refused(self, stopped_driver: Path) -> None:
        result = _run(
            stopped_driver,
            "-Common",
            str(COMMON),
            "-Before",
            "27372",
            "-After",
            "31005",
            "-AlivePid",
            "31005",
        )
        flat = _flat(result)
        assert result.returncode == 1, flat
        assert "captured worker pid 31005 is still alive" in flat, flat

    def test_cim_denied_falls_back_to_the_unique_service_listener(
        self, worker_fallback_driver: Path
    ) -> None:
        result = _run(worker_fallback_driver, "-Common", str(COMMON))
        flat = _flat(result)
        assert result.returncode == 0, flat
        assert "WORKER=4312" in flat, flat

    def test_dotenv_pins_operator_default_for_localsystem_and_target_cli(
        self, state_binding_driver: Path, tmp_path: Path
    ) -> None:
        app = tmp_path / "app"
        app.mkdir()
        (app / ".env").write_text("EXOMEM_VAULT_PATH=vault\n", encoding="utf-8")
        operator_local = tmp_path / "operator-local"
        system_local = tmp_path / "systemprofile-local"
        result = _run(
            state_binding_driver,
            "-Common",
            str(COMMON),
            "-AppDirectory",
            str(app),
            "-OperatorLocal",
            str(operator_local),
            "-SystemLocal",
            str(system_local),
        )
        flat = _flat(result)
        expected = operator_local / "exomem" / "state"
        assert result.returncode == 0, flat
        assert f"BOUND={expected}" in flat, flat
        dotenv = (app / ".env").read_text(encoding="utf-8")
        assert f"EXOMEM_STATE_ROOT={expected}" in dotenv
        assert str(system_local) not in dotenv

    def test_explicit_stopped_resume_requires_the_configured_listener_unbound(
        self, stopped_resume_driver: Path
    ) -> None:
        refused = _run(
            stopped_resume_driver,
            "-Common",
            str(COMMON),
            "-ListenerPid",
            "4312",
        )
        accepted = _run(stopped_resume_driver, "-Common", str(COMMON))

        assert refused.returncode == 1, _flat(refused)
        assert "listener port 8765 is still owned by pid(s): 4312" in _flat(refused)
        assert accepted.returncode == 0, _flat(accepted)
        assert "DRIVER-COMPLETED" in _flat(accepted)

    def test_resume_requires_the_exact_durable_receipt(
        self,
        receipt_resume_driver: Path,
        tmp_path: Path,
    ) -> None:
        receipt_root = tmp_path / "receipts"
        binding = tmp_path / "service" / ".env"
        binding.parent.mkdir()
        binding.write_text("EXOMEM_STATE_ROOT=unused\n", encoding="utf-8")
        state_root = tmp_path / "state"
        vault = tmp_path / "vault"
        python = Path(os.environ.get("PYTHON", os.sys.executable))
        args = (
            "-Common",
            str(COMMON),
            "-Python",
            str(python),
            "-ReceiptRoot",
            str(receipt_root),
            "-BindingPath",
            str(binding),
            "-StateRoot",
            str(state_root),
            "-Vault",
            str(vault),
        )

        missing = _run(receipt_resume_driver, *args, "-Mode", "resume")
        assert missing.returncode == 1, _flat(missing)
        assert "receipt" in _flat(missing).lower()

        created = _run(
            receipt_resume_driver,
            *args,
            "-Mode",
            "create",
            "-CapturedPid",
            "2147483000",
        )
        assert created.returncode == 0, _flat(created)

        wrong_args = list(args)
        wrong_args[wrong_args.index("-StateRoot") + 1] = str(tmp_path / "other-state")
        wrong_root = _run(
            receipt_resume_driver,
            *wrong_args,
            "-Mode",
            "resume",
        )
        assert wrong_root.returncode == 1, _flat(wrong_root)
        assert "does not match" in _flat(wrong_root).lower()

        receipt_identity = (
            "--path",
            str(receipt_root / "exomem.json"),
            "--service-id",
            "exomem",
            "--binding-path",
            str(binding),
            "--state-root",
            str(state_root),
            "--vault",
            str(vault),
            "--target-port",
            "8765",
        )
        starting = subprocess.run(
            [
                os.sys.executable,
                str(TRANSITION_TOOL),
                "phase",
                *receipt_identity,
                "--phase",
                "starting",
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        assert starting.returncode == 0, starting.stderr
        incomplete = _run(receipt_resume_driver, *args, "-Mode", "resume")
        assert incomplete.returncode == 1, _flat(incomplete)
        assert "incomplete start" in _flat(incomplete).lower()

        failed = subprocess.run(
            [
                os.sys.executable,
                str(TRANSITION_TOOL),
                "phase",
                *receipt_identity,
                "--phase",
                "failed",
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        assert failed.returncode == 0, failed.stderr
        accepted = _run(receipt_resume_driver, *args, "-Mode", "resume")
        assert accepted.returncode == 0, _flat(accepted)
        assert "DRIVER-COMPLETED" in _flat(accepted)

    def test_detached_captured_writer_blocks_resume_until_its_commit_finishes(
        self,
        receipt_resume_driver: Path,
        tmp_path: Path,
    ) -> None:
        database = tmp_path / "legacy.sqlite"
        child = subprocess.Popen(
            [
                os.sys.executable,
                "-c",
                (
                    "import sqlite3,sys; db=sqlite3.connect(sys.argv[1]); "
                    "db.execute('create table writes(value text)'); "
                    "db.execute(\"insert into writes values ('before')\"); db.commit(); "
                    "print('READY', flush=True); assert sys.stdin.readline().strip()=='commit'; "
                    "db.execute(\"insert into writes values ('after')\"); db.commit(); "
                    "print('COMMITTED', flush=True); db.close()"
                ),
                str(database),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        assert child.stdin is not None and child.stdout is not None
        assert child.stdout.readline().strip() == "READY"
        receipt_root = tmp_path / "receipts"
        binding = tmp_path / "service" / ".env"
        binding.parent.mkdir()
        binding.write_text("EXOMEM_STATE_ROOT=unused\n", encoding="utf-8")
        common = (
            "-Common",
            str(COMMON),
            "-Python",
            os.sys.executable,
            "-ReceiptRoot",
            str(receipt_root),
            "-BindingPath",
            str(binding),
            "-StateRoot",
            str(tmp_path / "state"),
            "-Vault",
            str(tmp_path / "vault"),
        )
        try:
            created = _run(
                receipt_resume_driver,
                *common,
                "-Mode",
                "create",
                "-CapturedPid",
                str(child.pid),
            )
            assert created.returncode == 0, _flat(created)
            refused = _run(receipt_resume_driver, *common, "-Mode", "resume")
            assert refused.returncode == 1, _flat(refused)
            assert f"captured transition pid {child.pid} is still alive" in _flat(refused)

            child.stdin.write("commit\n")
            child.stdin.flush()
            assert child.stdout.readline().strip() == "COMMITTED"
            _, stderr = child.communicate(timeout=10)
            assert child.returncode == 0, stderr

            accepted = _run(receipt_resume_driver, *common, "-Mode", "resume")
            assert accepted.returncode == 0, _flat(accepted)
            import sqlite3

            with sqlite3.connect(database) as reader:
                assert reader.execute("select value from writes order by rowid").fetchall() == [
                    ("before",),
                    ("after",),
                ]
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=10)

    @pytest.mark.parametrize("receipt_phase", ("starting", "started"))
    def test_failed_start_publishes_new_listener_before_resume_can_be_authorized(
        self,
        failed_start_receipt_driver: Path,
        receipt_resume_driver: Path,
        tmp_path: Path,
        receipt_phase: str,
    ) -> None:
        database = tmp_path / "failed-start.sqlite"
        child = subprocess.Popen(
            [
                os.sys.executable,
                "-c",
                (
                    "import socket,sqlite3,sys; "
                    "db=sqlite3.connect(sys.argv[1]); "
                    "db.execute('create table writes(value text)'); "
                    "db.execute(\"insert into writes values ('before')\"); db.commit(); "
                    "listener=socket.socket(); listener.bind(('127.0.0.1',0)); "
                    "listener.listen(); print(listener.getsockname()[1], flush=True); "
                    "assert sys.stdin.readline().strip()=='close'; listener.close(); "
                    "print('CLOSED', flush=True); "
                    "assert sys.stdin.readline().strip()=='commit'; "
                    "db.execute(\"insert into writes values ('after')\"); db.commit(); "
                    "print('COMMITTED', flush=True); db.close()"
                ),
                str(database),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        assert child.stdin is not None and child.stdout is not None
        target_port = int(child.stdout.readline().strip())
        with socket.socket() as reserved:
            reserved.bind(("127.0.0.1", 0))
            original_port = int(reserved.getsockname()[1])

        receipt_root = tmp_path / "receipts"
        binding = tmp_path / "service" / ".env"
        binding.parent.mkdir()
        binding.write_text("EXOMEM_STATE_ROOT=unused\n", encoding="utf-8")
        state_root = tmp_path / "state"
        vault = tmp_path / "vault"
        receipt = _create_starting_receipt(
            receipt_root=receipt_root,
            binding=binding,
            state_root=state_root,
            vault=vault,
            original_port=original_port,
            target_port=target_port,
            phase=receipt_phase,
        )
        target_worker_pid = 2147482999
        common = (
            "-Common",
            str(COMMON),
            "-Python",
            os.sys.executable,
            "-ReceiptRoot",
            str(receipt_root),
            "-BindingPath",
            str(binding),
            "-StateRoot",
            str(state_root),
            "-Vault",
            str(vault),
            "-TargetPort",
            str(target_port),
        )
        try:
            published = _run(
                failed_start_receipt_driver,
                *common,
                "-OriginalPort",
                str(original_port),
                "-TargetWorkerPid",
                str(target_worker_pid),
                "-TargetListenerPid",
                str(child.pid),
                "-Mode",
                "publish",
            )
            assert published.returncode == 0, _flat(published)
            payload = json.loads(receipt.read_text(encoding="utf-8"))
            assert payload["phase"] == "failed"
            assert target_worker_pid in payload["observed_pids"]
            assert child.pid in payload["observed_pids"]
            proof_pids = set(payload["captured_pids"] + payload["observed_pids"])
            assert target_worker_pid in proof_pids
            assert child.pid in proof_pids

            child.stdin.write("close\n")
            child.stdin.flush()
            assert child.stdout.readline().strip() == "CLOSED"
            refused = _run(receipt_resume_driver, *common, "-Mode", "resume")
            assert refused.returncode == 1, _flat(refused)
            assert f"captured transition pid {child.pid} is still alive" in _flat(refused)

            child.stdin.write("commit\n")
            child.stdin.flush()
            assert child.stdout.readline().strip() == "COMMITTED"
            _, stderr = child.communicate(timeout=10)
            assert child.returncode == 0, stderr

            accepted = _run(receipt_resume_driver, *common, "-Mode", "resume")
            assert accepted.returncode == 0, _flat(accepted)
            import sqlite3

            with sqlite3.connect(database) as reader:
                assert reader.execute("select value from writes order by rowid").fetchall() == [
                    ("before",),
                    ("after",),
                ]
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=10)

    @pytest.mark.parametrize("receipt_phase", ("starting", "started"))
    @pytest.mark.parametrize(
        ("mode", "error"),
        [
            ("enumeration-unavailable", "injected listener enumeration unavailable"),
            ("listener-hidden", "without an attributable process id"),
            ("listener-ambiguous", "without an attributable process id"),
            ("receipt-write-failure", "injected receipt write failure"),
            ("failed-phase-write-failure", "injected failed-phase write failure"),
        ],
    )
    def test_failed_start_proof_failure_retains_non_resumable_preaccepted_phase(
        self,
        failed_start_receipt_driver: Path,
        receipt_resume_driver: Path,
        tmp_path: Path,
        mode: str,
        error: str,
        receipt_phase: str,
    ) -> None:
        case = tmp_path / receipt_phase / mode
        case.mkdir(parents=True)
        with socket.socket() as original_socket, socket.socket() as target_socket:
            original_socket.bind(("127.0.0.1", 0))
            target_socket.bind(("127.0.0.1", 0))
            original_port = int(original_socket.getsockname()[1])
            target_port = int(target_socket.getsockname()[1])
        binding = case / "service" / ".env"
        binding.parent.mkdir()
        binding.write_text("EXOMEM_STATE_ROOT=unused\n", encoding="utf-8")
        receipt = _create_starting_receipt(
            receipt_root=case / "receipts",
            binding=binding,
            state_root=case / "state",
            vault=case / "vault",
            original_port=original_port,
            target_port=target_port,
            phase=receipt_phase,
        )
        result = _run(
            failed_start_receipt_driver,
            "-Common",
            str(COMMON),
            "-Python",
            os.sys.executable,
            "-ReceiptRoot",
            str(case / "receipts"),
            "-BindingPath",
            str(binding),
            "-StateRoot",
            str(case / "state"),
            "-Vault",
            str(case / "vault"),
            "-OriginalPort",
            str(original_port),
            "-TargetPort",
            str(target_port),
            "-TargetWorkerPid",
            "2147482999",
            "-Mode",
            mode,
        )
        assert result.returncode == 1, _flat(result)
        assert error in _flat(result)
        assert json.loads(receipt.read_text(encoding="utf-8"))["phase"] == receipt_phase
        refused = _run(
            receipt_resume_driver,
            "-Common",
            str(COMMON),
            "-Python",
            os.sys.executable,
            "-ReceiptRoot",
            str(case / "receipts"),
            "-BindingPath",
            str(binding),
            "-StateRoot",
            str(case / "state"),
            "-Vault",
            str(case / "vault"),
            "-TargetPort",
            str(target_port),
            "-Mode",
            "resume",
        )
        assert refused.returncode == 1, _flat(refused)
        assert "incomplete start" in _flat(refused).lower()


class TestInstallerLiveAcceptance:
    def test_process_tree_membership_accepts_a_nested_listener(
        self, process_tree_driver: Path
    ) -> None:
        result = _run(
            process_tree_driver,
            "-Common",
            str(COMMON),
            "-RootPid",
            "4101",
            "-CandidatePid",
            "4103",
            "-ParentMap",
            "4103:4102,4102:4101,4101:100",
        )
        assert result.returncode == 0, _flat(result)

    def test_process_tree_membership_refuses_an_unrelated_listener(
        self, process_tree_driver: Path
    ) -> None:
        result = _run(
            process_tree_driver,
            "-Common",
            str(COMMON),
            "-RootPid",
            "4101",
            "-CandidatePid",
            "4103",
            "-ParentMap",
            "4103:4102,4102:9999",
        )
        assert result.returncode == 1, _flat(result)
        assert "NOT-MEMBER" in _flat(result)

    def test_process_tree_membership_refuses_a_missing_root(
        self, process_tree_driver: Path
    ) -> None:
        result = _run(
            process_tree_driver,
            "-Common",
            str(COMMON),
            "-RootPid",
            "4101",
            "-CandidatePid",
            "4103",
            "-ParentMap",
            "4103:4102,4102:4101",
        )
        assert result.returncode == 1, _flat(result)
        assert "NOT-MEMBER" in _flat(result)

    def test_process_tree_membership_refuses_a_reused_root_pid(
        self, process_tree_driver: Path
    ) -> None:
        result = _run(
            process_tree_driver,
            "-Common",
            str(COMMON),
            "-RootPid",
            "4101",
            "-CandidatePid",
            "4103",
            "-ParentMap",
            "4103:4102,4102:4101,4101:100",
            "-RootIsNewer",
        )
        assert result.returncode == 1, _flat(result)
        assert "NOT-MEMBER" in _flat(result)

    def test_fresh_doctor_failure_leaves_no_autostart_service_and_retry_is_fresh(
        self, tmp_path: Path
    ) -> None:
        driver = tmp_path / "fresh-install-doctor-failure-driver.ps1"
        _write_executable(driver, _FRESH_INSTALL_DOCTOR_FAILURE_DRIVER)

        result = _run(
            driver,
            "-InstallScript",
            str(INSTALL_SERVICE),
            "-Common",
            str(COMMON),
            "-Python",
            os.sys.executable,
            "-TestRoot",
            str(tmp_path / "case"),
        )
        flat = _flat(result)
        assert result.returncode == 0, flat
        assert "FIRST_ERROR=Remote doctor preflight failed" in flat
        assert "FIRST_REGISTERED=False" in flat, flat
        assert "FIRST_AUTOSTART=False" in flat, flat
        assert "FIRST_RECEIPT=False" in flat, flat
        assert "NORMAL_RETRY=completed" in flat, flat
        assert "NORMAL_RETRY_STATUS=Running" in flat, flat
        assert "Could not prove the failed transition stopped" not in flat, flat

    def test_mcp_failure_cleanup_captures_a_replacement_writer_before_stopping(
        self, tmp_path: Path
    ) -> None:
        import sqlite3

        database = tmp_path / "mcp-failure.sqlite"
        stop_signal = tmp_path / "stop"
        closed_signal = tmp_path / "closed"
        commit_signal = tmp_path / "commit"
        child = subprocess.Popen(
            [
                os.sys.executable,
                "-c",
                (
                    "import pathlib,socket,sqlite3,sys,time; "
                    "db=sqlite3.connect(sys.argv[1]); "
                    "db.execute('create table writes(value text)'); "
                    "db.execute(\"insert into writes values ('before')\"); db.commit(); "
                    "listener=socket.socket(); listener.bind(('127.0.0.1',0)); "
                    "listener.listen(); print(listener.getsockname()[1], flush=True); "
                    "stop=pathlib.Path(sys.argv[2]); closed=pathlib.Path(sys.argv[3]); "
                    "commit=pathlib.Path(sys.argv[4]); "
                    "\nwhile not stop.exists(): time.sleep(.01)\n"
                    "listener.close(); closed.write_text('closed'); print('CLOSED', flush=True); "
                    "\nwhile not commit.exists(): time.sleep(.01)\n"
                    "db.execute(\"insert into writes values ('after')\"); db.commit(); "
                    "print('COMMITTED', flush=True); db.close()"
                ),
                str(database),
                str(stop_signal),
                str(closed_signal),
                str(commit_signal),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        assert child.stdout is not None
        target_port = int(child.stdout.readline().strip())
        with socket.socket() as reserved:
            reserved.bind(("127.0.0.1", 0))
            original_port = int(reserved.getsockname()[1])

        receipt_root = tmp_path / "receipts"
        binding = tmp_path / "service" / ".env"
        binding.parent.mkdir()
        binding.write_text("EXOMEM_STATE_ROOT=unused\n", encoding="utf-8")
        state_root = tmp_path / "state"
        vault = tmp_path / "vault"
        accepted_worker_pid = 2147483000
        receipt = _create_starting_receipt(
            receipt_root=receipt_root,
            binding=binding,
            state_root=state_root,
            vault=vault,
            original_port=original_port,
            target_port=target_port,
            phase="started",
        )
        driver = tmp_path / "install-mcp-failure-cleanup-driver.ps1"
        # The driver parses and executes the installer's actual MCP try/catch and
        # global cleanup function, so reintroducing an inner stop reopens the race.
        _write_executable(driver, _INSTALL_MCP_FAILURE_CLEANUP_DRIVER)

        try:
            result = _run(
                driver,
                "-InstallScript",
                str(INSTALL_SERVICE),
                "-Common",
                str(COMMON),
                "-Python",
                os.sys.executable,
                "-ReceiptRoot",
                str(receipt_root),
                "-BindingPath",
                str(binding),
                "-StateRoot",
                str(state_root),
                "-Vault",
                str(vault),
                "-TargetPort",
                str(target_port),
                "-AcceptedWorkerPid",
                str(accepted_worker_pid),
                "-ReplacementPid",
                str(child.pid),
                "-StopSignal",
                str(stop_signal),
                "-ClosedSignal",
                str(closed_signal),
            )
            flat = _flat(result)
            assert result.returncode == 0, flat
            payload = json.loads(receipt.read_text(encoding="utf-8"))
            proof_pids = set(payload["captured_pids"] + payload["observed_pids"])
            assert payload["phase"] == "failed"
            assert accepted_worker_pid in proof_pids
            assert child.pid in proof_pids, (
                f"replacement P2={child.pid} omitted from {sorted(proof_pids)}; {flat}"
            )
            assert "RESUME_REFUSED=" in flat
            assert "RESUME_AUTHORIZED" not in flat
            assert child.stdout.readline().strip() == "CLOSED"

            commit_signal.write_text("commit", encoding="ascii")
            assert child.stdout.readline().strip() == "COMMITTED"
            _, stderr = child.communicate(timeout=10)
            assert child.returncode == 0, stderr
            with sqlite3.connect(database) as reader:
                assert reader.execute("select value from writes order by rowid").fetchall() == [
                    ("before",),
                    ("after",),
                ]
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=10)

    def test_listener_must_belong_to_the_new_worker(
        self, live_acceptance_driver: Path
    ) -> None:
        mismatch = _run(
            live_acceptance_driver,
            "-Common",
            str(COMMON),
            "-WorkerPid",
            "4101",
            "-ListenerPid",
            "4102",
            "-ExpectedVersion",
            "1.2.3",
            "-ServedVersion",
            "1.2.3",
        )
        assert mismatch.returncode == 1, _flat(mismatch)
        assert "listener" in _flat(mismatch).lower()

    def test_descendant_listener_belongs_to_the_new_worker_tree(
        self, live_acceptance_driver: Path
    ) -> None:
        accepted = _run(
            live_acceptance_driver,
            "-Common",
            str(COMMON),
            "-WorkerPid",
            "4101",
            "-ListenerPid",
            "4102",
            "-ListenerIsDescendant",
            "-ExpectedVersion",
            "1.2.3",
            "-ServedVersion",
            "1.2.3",
        )
        assert accepted.returncode == 0, _flat(accepted)

    def test_listener_refuses_a_stale_service_worker_root(
        self, live_acceptance_driver: Path
    ) -> None:
        refused = _run(
            live_acceptance_driver,
            "-Common",
            str(COMMON),
            "-WorkerPid",
            "4101",
            "-ListenerPid",
            "4102",
            "-ListenerIsDescendant",
            "-CurrentWorkerPid",
            "4999",
            "-ExpectedVersion",
            "1.2.3",
            "-ServedVersion",
            "1.2.3",
        )
        assert refused.returncode == 1, _flat(refused)
        assert "listener" in _flat(refused).lower()

    def test_listener_identity_is_rechecked_after_ancestry_proof(
        self, live_acceptance_driver: Path
    ) -> None:
        refused = _run(
            live_acceptance_driver,
            "-Common",
            str(COMMON),
            "-WorkerPid",
            "4101",
            "-ListenerPid",
            "4102",
            "-ListenerIsDescendant",
            "-ListenerChangesAfterProof",
            "-ExpectedVersion",
            "1.2.3",
            "-ServedVersion",
            "1.2.3",
        )
        assert refused.returncode == 1, _flat(refused)
        assert "listener" in _flat(refused).lower()

    def test_health_must_equal_the_target_interpreter_version(
        self, live_acceptance_driver: Path
    ) -> None:
        mismatch = _run(
            live_acceptance_driver,
            "-Common",
            str(COMMON),
            "-WorkerPid",
            "4101",
            "-ListenerPid",
            "4101",
            "-ExpectedVersion",
            "1.2.3",
            "-ServedVersion",
            "1.2.2",
        )
        accepted = _run(
            live_acceptance_driver,
            "-Common",
            str(COMMON),
            "-WorkerPid",
            "4101",
            "-ListenerPid",
            "4101",
            "-ExpectedVersion",
            "1.2.3",
            "-ServedVersion",
            "1.2.3",
        )
        assert mismatch.returncode == 1, _flat(mismatch)
        assert "version" in _flat(mismatch).lower()
        assert accepted.returncode == 0, _flat(accepted)


class TestAcceleratorProbe:
    @pytest.fixture()
    def probe(self, tmp_path: Path):
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        # Both forms on every platform: PowerShell resolves through PATHEXT to
        # the .cmd and never the extensionless file.
        _write_executable(bin_dir / "python", _PYTHON_SH)
        _write_executable(bin_dir / "python.cmd", _PYTHON_CMD)
        python = bin_dir / ("python.cmd" if os.name == "nt" else "python")
        script = tmp_path / "torch-driver.ps1"
        _write_executable(script, _TORCH_DRIVER)

        def run(torch_version: str | None):
            env = os.environ.copy()
            env["FAKE_TORCH_VERSION"] = torch_version or ""
            return _flat(_run(script, "-Common", str(COMMON), "-Python", str(python), env=env))

        return run

    @pytest.mark.parametrize(
        "version",
        ["2.13.0+cu132", "2.9.1+rocm6.2", "2.8.0+xpu"],
    )
    def test_an_accelerator_build_is_detected(self, probe, version: str) -> None:
        flat = probe(version)
        assert "ACCEL=True" in flat, flat
        assert f"VERSION={version}" in flat, flat

    def test_a_plain_pypi_wheel_reads_as_cpu_only(self, probe) -> None:
        """No local tag is exactly what a silent CPU downgrade looks like."""
        flat = probe("2.13.0")
        assert "ACCEL=False" in flat, flat

    def test_absent_torch_is_not_an_accelerator(self, probe) -> None:
        flat = probe(None)
        assert "ACCEL=False" in flat, flat


class TestScriptsAreWired:
    """The helpers only matter if the scripts actually call them."""

    def test_restart_asserts_the_worker_changed_around_the_restart(self) -> None:
        body = RESTART.read_text(encoding="utf-8")
        assert "Get-ExomemServiceWorkerPid" in body
        assert "Assert-ExomemServiceRestarted" in body
        # The baseline is worthless if it is read after the stop.
        assert body.index("$workerBefore = Get-ExomemServiceWorkerPid") < body.index(
            "sc.exe stop"
        ), "the pre-restart worker pid must be captured before the service stops"

    def test_restart_resolves_the_log_dir_instead_of_assuming_the_checkout(self) -> None:
        """#569 moved logs off <repo>/logs; the script kept its own stale copy."""
        body = RESTART.read_text(encoding="utf-8")
        assert "Get-ExomemLogDir" in body
        assert '$logDir = Join-Path (Split-Path -Parent $PSScriptRoot) "logs"' not in body.split(
            "Write-Warning"
        )[0], "the checkout path must be a fallback, not the primary resolution"

    def test_deploy_probes_torch_rather_than_a_key_install_info_never_emits(self) -> None:
        body = DEPLOY.read_text(encoding="utf-8")
        assert "Test-ExomemAcceleratedTorch" in body
        assert "$before.accelerated" not in body
        assert "$after.accelerated" not in body

    def test_state_root_transition_stops_before_install_and_migrates_before_start(
        self,
    ) -> None:
        body = DEPLOY.read_text(encoding="utf-8")
        anchor = body.index("2. STATE-ROOT MAIN TRANSITION")
        stop = body.index("sc.exe stop", anchor)
        prove_gone = body.index("Assert-ExomemServiceStopped", stop)
        install = body.index("uv pip install")
        migrate = body.index("--migrate-state", install)
        doctor = body.index("-m\", \"exomem\", \"doctor", migrate)
        start = body.index("sc.exe start", doctor)
        prove_new_pid = body.index("Assert-ExomemServiceRestarted", start)
        assert stop < prove_gone < install < migrate < doctor < start < prove_new_pid

    def test_upgrade_disallows_skip_restart_before_any_install(self) -> None:
        body = UPGRADE.read_text(encoding="utf-8")
        refusal = body.index("-SkipRestart is unavailable during state-root migration")
        assert refusal < body.index("Install-ExomemPackage")

    def test_deploy_dry_run_exits_before_persisting_the_state_root(self) -> None:
        body = DEPLOY.read_text(encoding="utf-8")
        dry_run_exit = body.index("DryRun: resolved target only, no changes made")
        persist = body.index("Ensure-ExomemDotenvStateRootBinding")
        assert dry_run_exit < persist

    def test_install_service_stops_before_package_replacement_and_migrates_before_start(
        self,
    ) -> None:
        body = INSTALL_SERVICE.read_text(encoding="utf-8")
        anchor = body.index("STATE-ROOT MAIN TRANSITION")
        stop = body.index("stop $ServiceName", anchor)
        prove_gone = body.index("Assert-ExomemServiceStopped", stop)
        install = body.index("$python = Install-ReleaseVenv", prove_gone)
        migrate = body.index("--migrate-state", install)
        doctor = body.index('"-m", "exomem", "doctor"', migrate)
        start = body.index("start $ServiceName", doctor)
        assert stop < prove_gone < install < migrate < doctor < start

    def test_upgrade_main_transition_is_stop_prove_install_migrate_doctor_start(
        self,
    ) -> None:
        body = UPGRADE.read_text(encoding="utf-8")
        anchor = body.index("STATE-ROOT MAIN TRANSITION")
        stop = body.index("sc.exe stop", anchor)
        prove_gone = body.index("Assert-ExomemServiceStopped", stop)
        install = body.index("Install-ExomemPackage", prove_gone)
        migrate = body.index("--migrate-state", install)
        doctor = body.index("& $ServicePy @doctorArgs", migrate)
        start = body.index("sc.exe start", doctor)
        assert stop < prove_gone < install < migrate < doctor < start

    def test_posix_upgrade_uses_the_same_offline_transition_and_recovery_contract(
        self,
    ) -> None:
        body = UPGRADE_SH.read_text(encoding="utf-8")
        assert "--resume-stopped-transition" in body
        assert "--skip-restart is unavailable during state-root migration" in body
        anchor = body.index("STATE-ROOT MAIN TRANSITION")
        stop = body.index("exomem_stop_service", anchor)
        prove = body.index("exomem_assert_service_stopped", stop)
        bind = body.index("exomem_bind_service_state_root", prove)
        install = body.index("uv pip install", bind)
        migrate = body.index("--migrate-state", install)
        doctor = body.index("-m exomem doctor", migrate)
        start = body.index("exomem_start_service", doctor)
        prove_new = body.index("exomem_assert_service_restarted", start)
        assert stop < prove < bind < install < migrate < doctor < start < prove_new

    def test_posix_install_update_stops_before_install_and_starts_only_after_doctor(
        self,
    ) -> None:
        body = INSTALL_SERVICE_SH.read_text(encoding="utf-8")
        assert "--resume-stopped-transition" in body
        anchor = body.index("STATE-ROOT MAIN TRANSITION")
        stop = body.index("exomem_stop_service", anchor)
        prove = body.index("exomem_assert_service_stopped", stop)
        bind = body.index("exomem_bind_service_state_root", prove)
        install = body.index("uv pip install", bind)
        migrate = body.index("--migrate-state", install)
        doctor = body.index("-m exomem doctor", migrate)
        start = body.index("exomem_start_service", doctor)
        prove_new = body.index("exomem_assert_service_restarted", start)
        live_health = body.index('HEALTH_URL="http://${VERIFY_HOST}:${PORT}/health"', prove_new)
        version_gate = body.index('[[ "$SERVED_VERSION" == "$INSTALLED_VERSION" ]]', live_health)
        assert (
            stop
            < prove
            < bind
            < install
            < migrate
            < doctor
            < start
            < prove_new
            < live_health
            < version_gate
        )

    def test_posix_common_proves_exact_pid_and_listener_absence(self) -> None:
        body = COMMON_SH.read_text(encoding="utf-8")
        assert "exomem_service_worker_pid" in body
        assert "exomem_assert_service_stopped" in body
        assert "exomem_assert_stopped_resume_authority" in body
        assert "exomem_listener_pids" in body
        assert '[[ -z "$output" ]] && return 0' in body
        assert '[[ -n "$pids" ]] || return 2' in body

    def test_every_desktop_transition_is_wired_to_a_durable_receipt(self) -> None:
        windows = (DEPLOY, UPGRADE, INSTALL_SERVICE)
        posix = (UPGRADE_SH, INSTALL_SERVICE_SH)
        for path in windows:
            body = path.read_text(encoding="utf-8")
            anchor = body.index("STATE-ROOT MAIN TRANSITION")
            listener_capture = body.index("Get-ExomemConfiguredListenerPids")
            new_receipt = body.index("New-ExomemTransitionReceipt", anchor)
            assert "Read-ExomemTransitionReceipt" in body
            assert "Set-ExomemTransitionReceiptPhase" in body
            assert "Remove-ExomemTransitionReceipt" in body
            stop = body.index(
                "stop $ServiceName" if path == INSTALL_SERVICE else "sc.exe stop",
                anchor,
            )
            assert listener_capture < new_receipt < stop
            assert "-ListenerPids $listenerPidsBefore" in body[new_receipt:stop]
            starting = body.index('-Phase "starting"', anchor)
            start = body.index(
                "start $ServiceName" if path == INSTALL_SERVICE else "sc.exe start",
                anchor,
            )
            assert starting < start
        for path in posix:
            body = path.read_text(encoding="utf-8")
            listener_capture = body.index("exomem_listener_pids")
            new_receipt = body.index("exomem_create_transition_receipt")
            stop = body.index("exomem_stop_service", new_receipt)
            assert listener_capture < new_receipt < stop
            assert '"$LISTENER_PIDS_BEFORE"' in body[new_receipt:stop]
            assert "exomem_create_transition_receipt" in body
            assert "exomem_assert_stopped_resume_authority" in body
            assert "exomem_update_transition_receipt" in body
            assert "exomem_clear_transition_receipt" in body
            starting = body.index('"$PORT" starting')
            assert starting < body.index("exomem_start_service", starting)
        assert "exomem_verify_transition_receipt" in COMMON_SH.read_text(
            encoding="utf-8"
        )

    def test_every_failure_cleanup_publishes_the_complete_failed_start_pid_union(
        self,
    ) -> None:
        for path in (DEPLOY, UPGRADE, INSTALL_SERVICE):
            body = path.read_text(encoding="utf-8")
            start = body.index("function Stop-FailedStateRootTransition")
            cleanup = body[start : body.index("\ntrap {", start)]
            assert "Publish-ExomemFailedTransitionReceipt" in cleanup, path
            assert 'Set-ExomemTransitionReceiptPhase @transitionIdentity -Phase "failed"' not in cleanup

        for path, function_name, trap_name in (
            (UPGRADE_SH, "cleanup_transition() {", "trap cleanup_transition EXIT"),
            (INSTALL_SERVICE_SH, "cleanup() {", "trap cleanup EXIT"),
        ):
            body = path.read_text(encoding="utf-8")
            start = body.index(function_name)
            cleanup = body[start : body.index(trap_name, start)]
            assert "exomem_publish_failed_transition_receipt" in cleanup, path
            assert "exomem_update_transition_receipt" not in cleanup, path

        common_ps = COMMON.read_text(encoding="utf-8")
        start = common_ps.index("function Publish-ExomemFailedTransitionReceipt")
        helper = common_ps[start : common_ps.index("\nfunction ", start + 1)]
        assert "Read-ExomemTransitionReceipt" in helper
        assert '@("starting", "started") -contains' in helper
        assert "@($receipt.port, $receipt.target_port)" in helper
        assert "Get-ExomemListenerPidsForPort" in helper
        assert "$proofPhase = [string]$receipt.phase" in helper
        proof_publish = helper.index("-Phase $proofPhase -ObservedPids $observedPids")
        failed_publish = helper.index('-Phase "failed"', proof_publish)
        assert proof_publish < failed_publish
        assert 'Set-ExomemTransitionReceiptPhase @identity -Phase "failed"' in helper

        common_sh = COMMON_SH.read_text(encoding="utf-8")
        start = common_sh.index("exomem_publish_failed_transition_receipt() {")
        helper = common_sh[start : common_sh.index("\n}\n", start) + 3]
        assert "exomem_transition_receipt_field" in helper
        assert '[[ "$phase" == "starting" || "$phase" == "started" ]]' in helper
        assert 'receipt_port="$(exomem_transition_receipt_field' in helper
        assert '"$receipt_port" "$target_port"' in helper
        assert "exomem_listener_pids" in helper
        assert "exomem_update_transition_receipt" in helper
        proof_publish = helper.index('"$target_port" "$phase" "$proof_pids"')
        failed_publish = helper.index('"$target_port" failed', proof_publish)
        assert proof_publish < failed_publish

        resume_ps = common_ps[common_ps.index(
            "function Assert-ExomemStoppedResumeAuthority"
        ) :]
        assert '@("starting", "started") -contains' in resume_ps
        resume_sh = common_sh.split(
            "exomem_assert_stopped_resume_authority() {", 1
        )[1]
        assert '[[ "$phase" == "starting" || "$phase" == "started" ]]' in resume_sh

    def test_worker_observation_cannot_advance_started_before_live_acceptance(
        self,
    ) -> None:
        for path in (DEPLOY, UPGRADE, INSTALL_SERVICE):
            body = path.read_text(encoding="utf-8")
            starting = body.index('Phase "starting"')
            worker = body.index("$workerAfter = Get-ExomemServiceWorkerPid", starting)
            worker_proof = body.index(
                'Phase "starting" -ObservedPids @($workerAfter)', worker
            )
            listener = body.index("Assert-ExomemListenerOwnedByWorker", worker_proof)
            version = body.index("Wait-ExomemHealthVersion", listener)
            started = body.index('Phase "started"', version)
            assert starting < worker < worker_proof < listener < version < started, path

        for path in (UPGRADE_SH, INSTALL_SERVICE_SH):
            body = path.read_text(encoding="utf-8")
            starting = body.index('"$PORT" starting')
            worker = body.index("WORKER_AFTER=", starting)
            worker_proof = body.index('"$PORT" starting "$WORKER_AFTER"', worker)
            listener = body.index("exomem_assert_listener_owned_by_worker", worker_proof)
            if path == UPGRADE_SH:
                version = body.index(
                    'if [[ -n "$AFTER" && "$SERVED" != "$AFTER" ]]', listener
                )
            else:
                version = body.index(
                    '[[ "$SERVED_VERSION" == "$INSTALLED_VERSION" ]]', listener
                )
            started = body.index('"$PORT" started', version)
            assert starting < worker < worker_proof < listener < version < started, path

    def test_windows_installer_accepts_only_exact_target_worker_and_version(self) -> None:
        body = INSTALL_SERVICE.read_text(encoding="utf-8")
        installed = body.index("Get-ExomemInstalledVersion")
        start = body.index("start $ServiceName", installed)
        listener = body.index("Assert-ExomemListenerOwnedByWorker", start)
        version = body.index("Wait-ExomemHealthVersion", listener)
        clear = body.index("Remove-ExomemTransitionReceipt", version)
        assert installed < start < listener < version < clear

    def test_posix_upgrade_controls_the_identity_declared_by_selected_unit(self) -> None:
        body = UPGRADE_SH.read_text(encoding="utf-8")
        assert 'SERVICE_ID="$(exomem_service_id "$UNIT_FILE")"' in body
        for call in (
            'exomem_service_worker_pid "$SERVICE_ID"',
            'exomem_stop_service "$SERVICE_ID"',
            'exomem_start_service "$UNIT_FILE" "$SERVICE_ID"',
            'exomem_wait_worker_pid 60 "$SERVICE_ID"',
        ):
            assert call in body

    def test_posix_installer_controls_the_rendered_service_identity(self) -> None:
        body = INSTALL_SERVICE_SH.read_text(encoding="utf-8")
        assert 'SERVICE_ID="$(exomem_service_id "$SERVICE_DEFINITION")"' in body
        assert 'RENDERED_SERVICE_ID="$(exomem_service_id "$SERVICE_DEFINITION")"' in body
        assert '[[ "$RENDERED_SERVICE_ID" == "$SERVICE_ID" ]]' in body
        assert 'exomem_start_service "$SERVICE_DEFINITION" "$SERVICE_ID"' in body

    def test_desktop_runbook_keeps_failure_stopped_and_proves_new_process(self) -> None:
        body = DEPLOYMENT_DOC.read_text(encoding="utf-8")
        assert "stop -> prove stopped/PID gone -> install target" in body
        assert "offline migrate -> doctor -> start -> prove new PID/version" in body
        assert "failure leaves the service stopped" in body

    def test_hosted_runbook_requires_zero_pods_and_target_image_migration_locking(
        self,
    ) -> None:
        deploy = HOSTED_DEPLOY_DOC.read_text(encoding="utf-8")
        upgrades = HOSTED_UPGRADE_DOC.read_text(encoding="utf-8")
        assert "routes closed and drained" in deploy
        assert "zero tenant runtime pods" in deploy
        assert "existing hosted lifetime lock" in deploy
        assert "new state-migration lock" in deploy
        assert "target image" in deploy
        assert "never roll back to the old image after state migration" in upgrades

    def test_hosted_target_image_job_enables_offline_state_migration(self) -> None:
        body = HOSTED_INIT_JOB.read_text(encoding="utf-8")
        assert "image: {{ .Values.image }}" in body
        assert "EXOMEM_HOSTED_OFFLINE_STATE_MIGRATION" in body
        assert 'eq .Values.workloadMode "initialize"' in body
        assert 'eq .Values.migrationMode "state-root-v1"' in body
