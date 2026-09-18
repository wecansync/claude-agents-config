[CmdletBinding()]
param(
  [Parameter(ValueFromRemainingArguments = $true)]
  [string[]] $Arguments
)
$ErrorActionPreference = 'Stop'
if ($Arguments -contains '--dry-run') {
  & (Join-Path $PSScriptRoot 'install.ps1') '--uninstall' @Arguments
} else {
  & (Join-Path $PSScriptRoot 'install.ps1') '--uninstall' '--apply' @Arguments
}
exit $LASTEXITCODE
