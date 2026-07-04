#requires -Version 5.1
<#
.SYNOPSIS
  Thin wrapper -> the cross-platform Python builder, scripts/rebuild-schema-zip.py.
.DESCRIPTION
  Assembles the claude.ai `.skill` zip from the public scaffold
  (src/exomem/_scaffold/_Schema), overlaying your real project-keys.yaml when --vault
  or $env:EXOMEM_VAULT_PATH is set. Requires Python (no Compress-Archive needed).
.EXAMPLE
  pwsh -File scripts/rebuild-schema-zip.ps1
#>
[CmdletBinding()]
param([Parameter(ValueFromRemainingArguments = $true)] $Args)

$ErrorActionPreference = 'Stop'
& python "$PSScriptRoot/rebuild-schema-zip.py" @Args
exit $LASTEXITCODE
