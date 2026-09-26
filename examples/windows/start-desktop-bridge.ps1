<#
Experimental opt-in: let Codex Desktop own an LTC-enabled App Server wrapper.
Close Codex Desktop yourself before starting. This script never stops an app.
-CheckOnly inspects paths and the Core version, then tests native GUI process
creation with its initial thread suspended. It immediately terminates only that
new probe process; it never runs GUI code, starts a server, or changes settings.
Requires the Windows-capable LTC installation and an authenticated profile.
-PackageContext opts into the Windows package diagnostic launcher. Its token is
not guaranteed equivalent to normal app activation. No persistent settings change.
#>
[CmdletBinding()]
param(
    [string]$Python,
    [string]$CodexHome = $(if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $env:USERPROFILE '.codex' }),
    [string]$CodexBin,
    [string]$CoreWrapper,
    [string]$DesktopExe,
    [switch]$PackageContext,
    [switch]$CheckOnly
)
$ErrorActionPreference = 'Stop'
$scriptDirectory = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $Python) {
    # Windows PowerShell may evaluate parameter defaults before PSScriptRoot
    # is populated when invoked with -File.
    $Python = Join-Path $scriptDirectory '..\..\.venv\Scripts\python.exe'
}

function Get-DesktopPackage {
    if ($PSVersionTable.PSEdition -eq 'Desktop') {
        return Get-AppxPackage -Name 'OpenAI.Codex' | Select-Object -First 1
    }
    # Some PowerShell 7 installations cannot load Appx. Query Windows
    # PowerShell rather than guessing a package path.
    $windowsPowerShell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    $text = & $windowsPowerShell -NoProfile -NonInteractive -Command "Get-AppxPackage -Name 'OpenAI.Codex' | Select-Object -First 1 Name,Version,InstallLocation,PackageFullName,PackageFamilyName | ConvertTo-Json -Compress"
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

function Get-DesktopLaunchProbe {
    $probeScript = Join-Path $scriptDirectory 'probe-desktop-launch.py'
    if (-not (Test-Path -LiteralPath $probeScript -PathType Leaf)) {
        throw 'The native launch probe is missing. Use the complete current checkout.'
    }
    $info = New-Object System.Diagnostics.ProcessStartInfo
    $info.FileName = $Python
    $info.Arguments = '-B "{0}" "{1}"' -f $probeScript, $DesktopExe
    $info.UseShellExecute = $false
    $info.CreateNoWindow = $true
    $info.RedirectStandardOutput = $true
    $info.RedirectStandardError = $true
    foreach ($key in $settings.Keys) {
        if ($null -eq $settings[$key]) {
            $info.EnvironmentVariables.Remove($key)
        } else {
            $info.EnvironmentVariables[$key] = $settings[$key]
        }
    }
    $probe = New-Object System.Diagnostics.Process
    $probe.StartInfo = $info
    try {
        if (-not $probe.Start()) { throw 'Cannot start the native Desktop launch probe.' }
        $output = $probe.StandardOutput.ReadToEndAsync()
        $errors = $probe.StandardError.ReadToEndAsync()
        if (-not $probe.WaitForExit(15000)) {
            # Do not kill a probe that might still be cleaning up its suspended
            # child. In particular, never substitute a PID-based GUI kill.
            throw "The launch probe did not finish cleanup (probe PID $($probe.Id)). No normal Desktop launch was attempted."
        }
        if ($probe.ExitCode -ne 0) {
            throw "The native launch probe failed or could not confirm cleanup: $($errors.Result.Trim())"
        }
        try { $result = $output.Result | ConvertFrom-Json } catch { throw 'Invalid native launch probe response.' }
        if ($result.can_create_suspended -isnot [bool] -or
            $result.cleanup_complete -ne $true -or $result.probe_resumed -ne $false) {
            throw 'The native launch probe did not confirm safe completion.'
        }
        return $result
    } finally {
        $probe.Dispose()
    }
}

function Invoke-PackageDesktop([bool]$ProbeOnly) {
    # Invoke-CommandInDesktopPackage does not guarantee inheritance of our
    # shell environment. Transfer only the seven intended overrides in a
    # temporary request, and apply them inside the package-identity helper.
    $basePython = (& $Python -I -c 'import sys; print(sys._base_executable)').Trim()
    if ($LASTEXITCODE -ne 0 -or -not $basePython) { throw 'Cannot find the base Python interpreter.' }
    $windowlessPython = Join-Path (Split-Path -Parent $basePython) 'pythonw.exe'
    if (-not (Test-Path -LiteralPath $windowlessPython -PathType Leaf)) {
        throw 'PackageContext requires pythonw.exe beside the base Python interpreter to keep the diagnostic helper hidden.'
    }
    $helper = Join-Path $scriptDirectory 'desktop-package-launch.py'
    $facade = Join-Path $scriptDirectory 'invoke-desktop-package.ps1'
    foreach ($file in @($helper, $facade)) {
        if (-not (Test-Path -LiteralPath $file -PathType Leaf)) { throw "Package launch helper is missing: $file" }
    }
    $requestDirectory = Join-Path ([IO.Path]::GetTempPath()) ('ltc-desktop-launch-' + [Guid]::NewGuid().ToString('N'))
    [void](New-Item -ItemType Directory -Path $requestDirectory)
    $requestFile = Join-Path $requestDirectory 'request.json'
    $resultFile = Join-Path $requestDirectory 'result.json'
    $request = @{
        expected_package_full_name = $package.PackageFullName
        desktop_executable = $DesktopExe
        environment = $settings
        result_file = $resultFile
        check_only = $ProbeOnly
    }
    $invoker = $null
    $helperFinished = $false
    try {
        [IO.File]::WriteAllText($requestFile, ($request | ConvertTo-Json -Depth 4), (New-Object Text.UTF8Encoding($false)))
        $info = New-Object System.Diagnostics.ProcessStartInfo
        $info.FileName = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
        $info.Arguments = '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "{0}" -PackageFamilyName "{1}" -Python "{2}" -Helper "{3}" -RequestFile "{4}"' -f $facade, $package.PackageFamilyName, $windowlessPython, $helper, $requestFile
        $info.UseShellExecute = $false
        $info.CreateNoWindow = $true
        $info.RedirectStandardOutput = $true
        $info.RedirectStandardError = $true
        $invoker = New-Object System.Diagnostics.Process
        $invoker.StartInfo = $info
        if (-not $invoker.Start()) { throw 'Cannot start the Windows package diagnostic launcher.' }
        $output = $invoker.StandardOutput.ReadToEndAsync()
        $errors = $invoker.StandardError.ReadToEndAsync()
        if (-not $invoker.WaitForExit(15000)) {
            throw "Package activation did not return. Do not retry until the helper's outcome is resolved. Diagnostic files: $requestDirectory"
        }
        if ($invoker.ExitCode -ne 0) {
            throw "Windows package activation failed: $($errors.Result.Trim()). Diagnostic files: $requestDirectory"
        }
        $deadline = [DateTime]::UtcNow.AddSeconds(20)
        $result = $null
        do {
            if (Test-Path -LiteralPath $resultFile -PathType Leaf) {
                try { $result = [IO.File]::ReadAllText($resultFile) | ConvertFrom-Json } catch { $result = $null }
            }
            if ($null -ne $result) { break }
            Start-Sleep -Milliseconds 100
        } while ([DateTime]::UtcNow -lt $deadline)
        if ($null -eq $result) {
            throw "Package helper did not report completion. Do not retry until its outcome is resolved. Diagnostic files: $requestDirectory"
        }
        $helperFinished = $true
        if ($result.ok -ne $true) {
            $nativeDetail = ''
            if ($null -ne $result.native_launch_probe) {
                $nativeDetail = " Win32 error $($result.native_launch_probe.native_error_code): $($result.native_launch_probe.native_error_message)."
            }
            throw "Package helper failed: $($result.error_type): $($result.message).$nativeDetail"
        }
        if ($result.package_full_name -ne $package.PackageFullName -or
            $result.native_launch_probe.cleanup_complete -ne $true -or
            $result.native_launch_probe.probe_resumed -ne $false) {
            throw 'Package helper did not confirm the expected identity and probe cleanup.'
        }
        return $result
    } finally {
        if ($null -ne $invoker) { $invoker.Dispose() }
        # Never remove input while an uncertain helper might still need it.
        # No recursive deletion: these are the two exact temporary files only.
        if ($helperFinished) {
            $cleanupComplete = $false
            $cleanupDeadline = [DateTime]::UtcNow.AddSeconds(3)
            do {
                try {
                    foreach ($file in @($requestFile, $resultFile)) {
                        if (Test-Path -LiteralPath $file -PathType Leaf) { Remove-Item -LiteralPath $file -Force }
                    }
                    [IO.Directory]::Delete($requestDirectory, $false)
                    $cleanupComplete = $true
                } catch {
                    # A complete result can be readable just before pythonw
                    # closes it. Retry sharing conflicts without masking launch.
                    Start-Sleep -Milliseconds 50
                }
            } while (-not $cleanupComplete -and [DateTime]::UtcNow -lt $cleanupDeadline)
            if (-not $cleanupComplete) { Write-Warning "Temporary diagnostic files were retained: $requestDirectory" }
        }
    }
}

$Python = (Resolve-Path -LiteralPath $Python).Path
$CodexHome = (Resolve-Path -LiteralPath $CodexHome).Path
if (-not $DesktopExe -or $PackageContext) {
    $package = Get-DesktopPackage
    if (-not $package) { throw 'Codex Desktop package not found; supply -DesktopExe with its actual executable.' }
    [xml]$manifest = Get-Content -LiteralPath (Join-Path $package.InstallLocation 'AppxManifest.xml') -Raw
    $application = $manifest.SelectSingleNode("/*[local-name()='Package']/*[local-name()='Applications']/*[local-name()='Application'][@Id='App']")
    if (-not $application -or -not $application.Executable) {
        throw 'Cannot identify the main Desktop application in its manifest; supply -DesktopExe explicitly.'
    }
    $manifestExecutable = Join-Path $package.InstallLocation $application.Executable
    if ($DesktopExe -and (Resolve-Path -LiteralPath $DesktopExe).Path -ine (Resolve-Path -LiteralPath $manifestExecutable).Path) {
        throw 'PackageContext accepts only the installed package manifest main executable.'
    }
    $DesktopExe = $manifestExecutable
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
    $packageResult = $null
    if ($PackageContext) {
        $packageResult = Invoke-PackageDesktop $true
        $launchProbe = $packageResult.native_launch_probe
    } else {
        $launchProbe = Get-DesktopLaunchProbe
    }
    $blockers = @()
    if (-not $launchProbe.can_create_suspended) { $blockers += "Native GUI creation failed: Win32 error $($launchProbe.native_error_code)." }
    if ($existing.Count -gt 0) { $blockers += 'Desktop is already running.' }
    if ($uninspectable.Count -gt 0) { $blockers += 'A matching Desktop process could not be inspected.' }
    if (Test-Path -LiteralPath $metadata) {
        try {
            if ((Get-BridgeStatus).running) { $blockers += 'A live bridge already exists.' }
        } catch {
            $blockers += "Existing bridge state could not be inspected: $($_.Exception.Message)"
        }
    }
    [pscustomobject]@{
        check_only = $true
        preflight_passed = ($blockers.Count -eq 0)
        native_creation_passed = $launchProbe.can_create_suspended
        launch_blockers = $blockers
        launch_mode = $(if ($PackageContext) { 'experimental package diagnostic context' } else { 'direct native creation' })
        verified_package_identity = $(if ($null -ne $packageResult) { $packageResult.package_full_name } else { $null })
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
        native_launch_probe = $launchProbe
        launch_validation = 'Only suspended native process creation was tested. GUI execution, wrapper inheritance, App Tools and original-session ACK remain unverified.'
    } | ConvertTo-Json -Depth 3
    if ($blockers.Count -gt 0) { exit 1 }
    return
}
if ($existing.Count -gt 0 -or $uninspectable.Count -gt 0) {
    throw 'Codex Desktop is running, or a matching process could not be inspected. Save work and close Desktop before switching its App Server connection. No process was stopped.'
}
if (Test-Path -LiteralPath $metadata) {
    $status = Get-BridgeStatus
    if ($status.running -eq $true) { throw 'A bridge already exists. Inspect it and use its documented connection, or stop it after its clients close.' }
}
if ($PackageContext) {
    Write-Output 'Using the experimental Windows package diagnostic context. It is not guaranteed equivalent to normal app activation.'
    $packageResult = Invoke-PackageDesktop $false
    if (-not $packageResult.native_launch_probe.can_create_suspended -or -not $packageResult.desktop_pid) {
        throw 'Package helper did not confirm native creation and a Desktop launch request.'
    }
    $deadline = [DateTime]::UtcNow.AddSeconds(20)
    $bridgeReady = $false
    do {
        if (Test-Path -LiteralPath $metadata -PathType Leaf) {
            try {
                $bridgeStatus = Get-BridgeStatus
                $bridgeReady = ($bridgeStatus.running -eq $true -and
                    $bridgeStatus.codex_home -ieq $CodexHome -and
                    $bridgeStatus.active_clients -ge 1)
            } catch { $bridgeReady = $false }
        }
        if ($bridgeReady) { break }
        Start-Sleep -Milliseconds 250
    } while ([DateTime]::UtcNow -lt $deadline)
    if (-not $bridgeReady) {
        throw "Desktop launch was requested (PID $($packageResult.desktop_pid)), but a live bridge was not observed. Do not assume callback support or retry blindly. Inspect $metadata and Desktop's startup logs; no existing process was stopped."
    }
    Write-Output "A live bridge with a connected client was observed for this Codex profile: $metadata"
    Write-Output 'Validate App Tools and original-session delivery/ACK before relying on this experimental startup path.'
    return
}
$launchProbe = Get-DesktopLaunchProbe
if (-not $launchProbe.can_create_suspended) {
    throw "Desktop native launch is unavailable (Win32 error $($launchProbe.native_error_code): $($launchProbe.native_error_message)). Executable: $DesktopExe. This launcher cannot yet start this packaged Desktop with its bridge environment. Repeating a restart or using an elevated shell is not a verified fix. No existing Desktop process was stopped and no server was started."
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
    # substitute ShellAppsFolder activation: override inheritance is unverified.
    $desktopProcess = [System.Diagnostics.Process]::Start($launchInfo)
    if (-not $desktopProcess) { throw 'Desktop process creation returned no process.' }
    $desktopProcess.Dispose()
} catch {
    $launchError = $_.Exception
    while ($launchError.InnerException) { $launchError = $launchError.InnerException }
    $errorCode = if ($launchError -is [System.ComponentModel.Win32Exception]) {
        "Win32 error $($launchError.NativeErrorCode)"
    } else {
        $launchError.GetType().FullName
    }
    throw "Desktop launch failed ($errorCode): $($launchError.Message). Executable: $DesktopExe. No server was prestarted and no existing Desktop process was stopped."
}
Write-Output 'Desktop launch was requested with its experimental Core wrapper. Open the original conversation and validate tools and callback delivery before relying on it.'
Write-Output "For LTC setup in another terminal, set CODEX_LONG_TASK_WAKEUP_DESKTOP_BRIDGE_FILE to: $metadata"
Write-Output 'Desktop owns the wrapper and its App Server. Closing Desktop closes its stdio connection and the wrapper-managed bridge.'
