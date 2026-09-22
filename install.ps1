[CmdletBinding()]
param(
  [Parameter(ValueFromRemainingArguments = $true)]
  [string[]] $Arguments
)
$ErrorActionPreference = 'Stop'
$bundle = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot 'bin/install.py')).Path
if (-not $Arguments -or $Arguments.Count -eq 0) {
  $Arguments = @('--apply')
}
$py = Get-Command py -ErrorAction SilentlyContinue
if ($py) {
  & $py.Source '-3' $bundle @Arguments
} else {
  $python = Get-Command python -ErrorAction Stop
  & $python.Source $bundle @Arguments
}
exit $LASTEXITCODE
