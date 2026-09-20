[CmdletBinding()]
param(
  [Parameter(ValueFromRemainingArguments = $true)]
  [string[]] $Arguments
)
$ErrorActionPreference = 'Stop'
if ($Arguments -contains '--dry-run' -or $Arguments -contains '--check') {
  & (Join-Path $PSScriptRoot 'install.ps1') @Arguments
} else {
  & (Join-Path $PSScriptRoot 'install.ps1') '--apply' @Arguments
}
exit $LASTEXITCODE
