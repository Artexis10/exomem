#requires -Version 5.1
<#
.SYNOPSIS
  Thin wrapper -> the cross-platform Python builder, scripts/rebuild-schema-zip.py.
.DESCRIPTION
  Rebuilds Knowledge Base/_Schema.zip (the claude.ai `.skill`) from the canonical,
  stripping the canonical's GENERIC markers (keeping your real content) so the zip
  carries no marker comments. Resolves the vault from --vault or
  $env:EXOMEM_VAULT_PATH. Requires Python (no Compress-Archive needed anymore).
.EXAMPLE
  pwsh -File scripts/rebuild-schema-zip.ps1
#>
[CmdletBinding()]
param([Parameter(ValueFromRemainingArguments = $true)] $Args)

$ErrorActionPreference = 'Stop'
& python "$PSScriptRoot/rebuild-schema-zip.py" @Args
exit $LASTEXITCODE
