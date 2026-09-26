<# Windows PowerShell 5.1 facade for the Appx diagnostic cmdlet. #>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$PackageFamilyName,
    [Parameter(Mandatory = $true)][string]$Python,
    [Parameter(Mandatory = $true)][string]$Helper,
    [Parameter(Mandatory = $true)][string]$RequestFile
)
$ErrorActionPreference = 'Stop'
# The main script selects the installed manifest's App identity. Only this
# windowless, short-lived diagnostic helper is explicitly given package context.
# Do not use PreventBreakaway: child ownership follows the normal Windows rules.
Invoke-CommandInDesktopPackage -PackageFamilyName $PackageFamilyName -AppId 'App' `
    -Command $Python -Args ('"{0}" --request "{1}"' -f $Helper, $RequestFile) | Out-Null
