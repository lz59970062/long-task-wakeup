<# Compatibility entry point; launcher resources ship inside the LTC package. #>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Python,
    [string]$CodexHome,
    [string]$CodexBin,
    [string]$CoreWrapper,
    [string]$DesktopExe,
    [string]$BridgeFile,
    [switch]$PackageContext,
    [switch]$CheckOnly
)
$ErrorActionPreference = 'Stop'
$resource = Join-Path (Split-Path -Parent $MyInvocation.MyCommand.Path) '..\..\src\long_task_callback\desktop_windows_assets\start-desktop-bridge.ps1'
if (-not (Test-Path -LiteralPath $resource -PathType Leaf)) {
    throw 'The checkout launcher resource is missing. Use the installed ltc desktop launch command.'
}
& $resource @PSBoundParameters
