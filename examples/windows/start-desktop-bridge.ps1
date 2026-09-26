<#
Experimental opt-in: let Codex Desktop own an LTC-enabled App Server wrapper.
Close Codex Desktop yourself before starting. This script never stops an app.
-CheckOnly inspects paths, the Core version and running GUI processes without
creating bridge state, starting a server, or changing the process environment.
Requires the Windows-capable LTC installation and an authenticated profile.
#>
[CmdletBinding()]
param(
    [string]$Python,
    [string]$CodexHome = $(if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $env:USERPROFILE '.codex' }),
    [string]$CodexBin,
    [string]$CoreWrapper,
    [string]$DesktopExe,
    [switch]$CheckOnly
)
$ErrorActionPreference = 'Stop'
if (-not $Python) {
    # Windows PowerShell may evaluate parameter defaults before PSScriptRoot
    # is populated when invoked with -File.
    $scriptDirectory = Split-Path -Parent $MyInvocation.MyCommand.Path
    $Python = Join-Path $scriptDirectory '..\..\.venv\Scripts\python.exe'
}

function Get-DesktopPackage {
    if ($PSVersionTable.PSEdition -eq 'Desktop') {
        return Get-AppxPackage -Name 'OpenAI.Codex' | Select-Object -First 1
    }
    # Some PowerShell 7 installations cannot load Appx. Query Windows
    # PowerShell rather than guessing a package path.
    $windowsPowerShell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    $text = & $windowsPowerShell -NoProfile -NonInteractive -Command "Get-AppxPackage -Name 'OpenAI.Codex' | Select-Object -First 1 Name,Version,InstallLocation | ConvertTo-Json -Compress"
    if ($LASTEXITCODE -ne 0) { throw 'Cannot inspect the Desktop package; supply -DesktopExe and -CodexBin explicitly.' }
    if ($text) { return $text | ConvertFrom-Json }
    return $null
}

function Resolve-Executable([string]$Value) {
    if (Test-Path -LiteralPath $Value -PathType Leaf) {
        return (Resolve-Path -LiteralPath $Value).Path
    }
    $command = Get-Command -Name $Value -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $command) { throw "Executable not found: $Value" }
    return $command.Source
}

function Get-BridgeStatus {
    $text = & $Python -B -m long_task_callback.desktop_bridge --codex-home $CodexHome --metadata-file $metadata --status
    if ($LASTEXITCODE -notin @(0, 1)) { throw 'Cannot safely inspect the existing bridge state.' }
    try { $status = $text | ConvertFrom-Json } catch { throw 'The bridge status response is invalid.' }
    if ($null -eq $status -or $status.running -isnot [bool]) { throw 'The bridge status response has no valid running state.' }
    return $status
}

function Get-WrapperVersion {
    # --version is transparently delegated to REAL_CODEX by the wrapper. Only
    # this child receives its override; the launching shell is never mutated.
    $info = New-Object System.Diagnostics.ProcessStartInfo
    $info.FileName = $CoreWrapper
    $info.Arguments = '--version'
    $info.UseShellExecute = $false
    $info.CreateNoWindow = $true
    $info.RedirectStandardOutput = $true
    $info.RedirectStandardError = $true
    $info.EnvironmentVariables['CODEX_LONG_TASK_WAKEUP_DESKTOP_REAL_CODEX'] = $CodexBin
    $info.EnvironmentVariables['CODEX_HOME'] = $CodexHome
    $info.EnvironmentVariables['PYTHONDONTWRITEBYTECODE'] = '1'
    $probe = New-Object System.Diagnostics.Process
    $probe.StartInfo = $info
    try {
        if (-not $probe.Start()) { throw 'Cannot start the Core wrapper version check.' }
        $output = $probe.StandardOutput.ReadToEndAsync()
        $errors = $probe.StandardError.ReadToEndAsync()
        if (-not $probe.WaitForExit(15000)) {
            $probe.Kill()
            $probe.WaitForExit()
            throw 'The Core wrapper version check timed out.'
        }
        $version = $output.Result.Trim()
        if ($probe.ExitCode -ne 0 -or $version -ne $coreVersion) {
            throw 'The Core wrapper did not report the selected Desktop Core version; reinstall the matching LTC package.'
        }
        return $version
    } finally {
        $probe.Dispose()
    }
}

$Python = (Resolve-Path -LiteralPath $Python).Path
$CodexHome = (Resolve-Path -LiteralPath $CodexHome).Path
if (-not $DesktopExe) {
    $package = Get-DesktopPackage
    if (-not $package) { throw 'Codex Desktop package not found; supply -DesktopExe with its actual executable.' }
    [xml]$manifest = Get-Content -LiteralPath (Join-Path $package.InstallLocation 'AppxManifest.xml') -Raw
    $application = $manifest.SelectSingleNode("/*[local-name()='Package']/*[local-name()='Applications']/*[local-name()='Application'][@Id='App']")
    if (-not $application -or -not $application.Executable) {
        throw 'Cannot identify the main Desktop application in its manifest; supply -DesktopExe explicitly.'
    }
    $DesktopExe = Join-Path $package.InstallLocation $application.Executable
}
$DesktopExe = (Resolve-Path -LiteralPath $DesktopExe).Path
$desktopDirectory = Split-Path -Parent $DesktopExe
$bundledCore = Join-Path $desktopDirectory 'resources\codex.exe'
$coreSource = 'explicit override'
if (-not $CodexBin) {
    if (-not (Test-Path -LiteralPath $bundledCore -PathType Leaf)) {
        throw 'The Desktop Core was not found beside the GUI; supply -CodexBin with the matching Core executable.'
    }
    $bundledInfo = Get-Item -LiteralPath $bundledCore
    $bundledHash = (Get-FileHash -LiteralPath $bundledCore -Algorithm SHA256).Hash
    $cacheRoot = Join-Path $env:LOCALAPPDATA 'OpenAI\Codex\bin'
    # Packaged resources may deny direct execution. Accept a per-user cache
    # copy only after hashing it against this Desktop's bundle; no PATH fallback.
    if (Test-Path -LiteralPath $cacheRoot -PathType Container) {
        $directories = Get-ChildItem -LiteralPath $cacheRoot -Directory | Sort-Object LastWriteTime -Descending | Select-Object -First 64
        foreach ($directory in $directories) {
            $candidate = Join-Path $directory.FullName 'codex.exe'
            if (-not (Test-Path -LiteralPath $candidate -PathType Leaf)) { continue }
            $candidateInfo = Get-Item -LiteralPath $candidate
            if ($candidateInfo.Length -ne $bundledInfo.Length) { continue }
            if ((Get-FileHash -LiteralPath $candidate -Algorithm SHA256).Hash -eq $bundledHash) {
                $CodexBin = $candidate
                $coreSource = 'Desktop cache copy with matching bundle SHA256'
                break
            }
        }
    }
    if (-not $CodexBin) {
        $CodexBin = $bundledCore
        $coreSource = 'Desktop bundled Core'
    }
}
$CodexBin = Resolve-Executable $CodexBin
if ([IO.Path]::GetExtension($CodexBin) -ine '.exe') {
    throw 'The Desktop wrapper requires the actual Core .exe; supply -CodexBin with the Desktop Core instead of a command shim.'
}
try {
    $coreVersion = ((& $CodexBin --version 2>&1) | Out-String).Trim()
    if ($LASTEXITCODE -ne 0 -or $coreVersion -notmatch '^codex-cli \S+$') { throw 'Unexpected Core version response.' }
} catch {
    throw 'The selected Core cannot be executed. Supply -CodexBin with a runnable Core matching this Desktop version; global CLI fallback is intentionally disabled.'
}
if (-not $CoreWrapper) {
    $CoreWrapper = Join-Path (Split-Path -Parent $Python) 'ltc-desktop-core.exe'
}
if (-not (Test-Path -LiteralPath $CoreWrapper -PathType Leaf)) {
    throw 'The ltc-desktop-core.exe wrapper is not installed beside the selected Python. Install the current LTC package or supply -CoreWrapper explicitly.'
}
$CoreWrapper = (Resolve-Path -LiteralPath $CoreWrapper).Path
if ([IO.Path]::GetExtension($CoreWrapper) -ine '.exe' -or $CoreWrapper -ieq $CodexBin) {
    throw 'The Core wrapper must be a separate Windows executable.'
}
$wrapperVersion = Get-WrapperVersion
$metadata = Join-Path $CodexHome 'long-task-wakeup\desktop-bridge.json'
$settings = @{
    CODEX_HOME = $CodexHome
    CODEX_CLI_PATH = $CoreWrapper
    CODEX_APP_SERVER_FORCE_CLI = '1'
    CODEX_APP_SERVER_WS_URL = $null
    CODEX_LONG_TASK_WAKEUP_DESKTOP_REAL_CODEX = $CodexBin
    CODEX_LONG_TASK_WAKEUP_DESKTOP_BRIDGE_FILE = $metadata
    CODEX_LONG_TASK_WAKEUP_DESKTOP_APP_SERVER = '1'
}
$guiPaths = @($DesktopExe, (Join-Path $desktopDirectory 'ChatGPT.exe'), (Join-Path $desktopDirectory 'Codex.exe'))
$guiNames = @('ChatGPT', 'Codex', [IO.Path]::GetFileNameWithoutExtension($DesktopExe)) | Select-Object -Unique
$existing = @()
$uninspectable = @()
foreach ($process in @(Get-Process -Name $guiNames -ErrorAction SilentlyContinue)) {
    $processPath = $null
    try { $processPath = $process.Path } catch { }
    if (-not $processPath) {
        $uninspectable += $process.Id
    } elseif ($guiPaths -contains $processPath) {
        $existing += $process.Id
    }
}
if ($CheckOnly) {
    [pscustomobject]@{
        check_only = $true
        python = $Python
        codex_home = $CodexHome
        desktop_executable = $DesktopExe
        core_executable = $CodexBin
        core_source = $coreSource
        core_version = $coreVersion
        core_wrapper = $CoreWrapper
        wrapper_core_version = $wrapperVersion
        bridge_metadata = $metadata
        child_environment = $settings
        desktop_running = ($existing.Count -gt 0)
        desktop_process_ids = $existing
        uninspectable_process_ids = $uninspectable
        launch_validation = 'Not performed. Packaged GUI direct launch, Desktop-created wrapper and fresh tool-pipe inheritance require an explicit restart test.'
    } | ConvertTo-Json -Depth 3
    return
}
if ($existing.Count -gt 0 -or $uninspectable.Count -gt 0) {
    throw 'Codex Desktop is running, or a matching process could not be inspected. Save work and close Desktop before switching its App Server connection. No process was stopped.'
}
if (Test-Path -LiteralPath $metadata) {
    $status = Get-BridgeStatus
    if ($status.running -eq $true) { throw 'A bridge already exists. Inspect it and use its documented connection, or stop it after its clients close.' }
}
# Desktop creates the wrapper itself and supplies fresh App Tools IPC settings
# and exact Core arguments. Never prestart an external server with stale IPC.
$launchInfo = New-Object System.Diagnostics.ProcessStartInfo
$launchInfo.FileName = $DesktopExe
$launchInfo.WorkingDirectory = $desktopDirectory
$launchInfo.UseShellExecute = $false
foreach ($key in $settings.Keys) {
    if ($null -eq $settings[$key]) {
        $launchInfo.EnvironmentVariables.Remove($key)
    } else {
        $launchInfo.EnvironmentVariables[$key] = $settings[$key]
    }
}
try {
    # UseShellExecute=false passes this child-only environment directly. Do not
    # substitute ShellAppsFolder activation, which does not preserve it.
    $desktopProcess = [System.Diagnostics.Process]::Start($launchInfo)
    if (-not $desktopProcess) { throw 'Desktop process creation returned no process.' }
    $desktopProcess.Dispose()
} catch {
    throw 'Desktop could not be started directly with the wrapper environment. No server was prestarted and no existing Desktop process was stopped.'
}
Write-Output 'Desktop launch was requested with its experimental Core wrapper. Open the original conversation and validate tools and callback delivery before relying on it.'
Write-Output "For LTC setup in another terminal, set CODEX_LONG_TASK_WAKEUP_DESKTOP_BRIDGE_FILE to: $metadata"
Write-Output 'Desktop owns the wrapper and its App Server. Closing Desktop closes its stdio connection and the wrapper-managed bridge.'
