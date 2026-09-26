# Fixed controller. Requests and replies are JSON, never localized CLI output.
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Console]::InputEncoding = New-Object System.Text.UTF8Encoding($false)
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)

function Error-Code($exception) {
    while ($null -ne $exception.InnerException) { $exception = $exception.InnerException }
    return [int]$exception.HResult
}

function Get-Task($folder, [string]$name) {
    try { return $folder.GetTask($name) }
    catch {
        $code = Error-Code $_.Exception
        if ($code -eq -2147024894 -or $code -eq -2147024893) { return $null }
        throw
    }
}

function Task-Status($task) {
    if ($null -eq $task) { return @{ registered = $false } }
    $instances = @()
    foreach ($instance in $task.GetInstances(0)) {
        $instances += @{ pid = [int]$instance.EnginePID; guid = [string]$instance.InstanceGuid;
                         state = [int]$instance.State }
    }
    # A registered-but-never-started task is ambiguous after a lost Run reply.
    return @{ registered = $true; state = [int]$task.State; instances = $instances;
              has_run = ($task.LastRunTime.Year -ge 2000 -and $task.LastTaskResult -ne 267011);
              last_result = [int]$task.LastTaskResult }
}

function Register-Task($service, $folder, $request, [string]$sid) {
    $configuration = $request.configuration
    $definition = $service.NewTask(0)
    $definition.RegistrationInfo.Description = 'Long Task Callback private worker'
    $definition.Principal.UserId = $sid
    $definition.Principal.LogonType = 3 # TASK_LOGON_INTERACTIVE_TOKEN
    $definition.Principal.RunLevel = 0 # TASK_RUNLEVEL_LUA
    $settings = $definition.Settings
    $settings.Enabled = [bool]$configuration.enabled
    $settings.AllowDemandStart = $true
    $settings.MultipleInstances = 2 # TASK_INSTANCES_IGNORE_NEW
    $settings.ExecutionTimeLimit = 'PT0S'
    $settings.RestartCount = 0
    $settings.DisallowStartIfOnBatteries = $false
    $settings.StopIfGoingOnBatteries = $false
    $settings.RunOnlyIfIdle = $false
    $settings.IdleSettings.StopOnIdleEnd = $false
    $settings.IdleSettings.RestartOnIdle = $false
    $settings.RunOnlyIfNetworkAvailable = $false
    $settings.StartWhenAvailable = $false
    $settings.WakeToRun = $false
    $settings.AllowHardTerminate = $true
    # Only the coordinator can opt into a login trigger. Business attempts cannot.
    if ([bool]$configuration.login) {
        $trigger = $definition.Triggers.Create(9) # TASK_TRIGGER_LOGON
        $trigger.UserId = $sid
        $trigger.Enabled = $true
    }
    $action = $definition.Actions.Create(0) # TASK_ACTION_EXEC
    $action.Path = [string]$configuration.executable
    $action.Arguments = [string]$configuration.arguments
    $action.WorkingDirectory = [string]$configuration.cwd
    $flags = 2 # TASK_CREATE: never replay an existing attempt after uncertainty.
    if ($request.operation -eq 'register' -and [bool]$request.replace) { $flags = 6 }
    $sddl = "D:P(A;;FA;;;SY)(A;;FA;;;$sid)"
    return $folder.RegisterTaskDefinition([string]$request.owner, $definition, $flags, $sid, $null, 3, $sddl)
}

try {
    $request = [Console]::In.ReadToEnd() | ConvertFrom-Json
    $service = New-Object -ComObject 'Schedule.Service'
    $service.Connect()
    $folder = $service.GetFolder('\')
    $sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    if ($request.operation -ne 'available' -and [string]$request.owner -notmatch '^[A-Za-z0-9][A-Za-z0-9_.-]{0,199}$') {
        throw 'invalid task owner'
    }
    switch ([string]$request.operation) {
        'available' { $response = @{ available = $true } }
        'status' { $response = Task-Status (Get-Task $folder $request.owner) }
        'definition' {
            $task = Get-Task $folder $request.owner
            if ($null -eq $task) { $response = @{ registered = $false } }
            else {
                $definition = $task.Definition
                $settings = $definition.Settings
                $response = @{ registered = $true; xml = [string]$task.Xml;
                    logon_type = [int]$definition.Principal.LogonType;
                    run_level = [int]$definition.Principal.RunLevel;
                    executable = [string]$definition.Actions.Item(1).Path;
                    trigger_count = [int]$definition.Triggers.Count;
                    multiple_instances = [int]$settings.MultipleInstances;
                    execution_time_limit = [string]$settings.ExecutionTimeLimit;
                    restart_count = [int]$settings.RestartCount;
                    disallow_batteries = [bool]$settings.DisallowStartIfOnBatteries;
                    stop_batteries = [bool]$settings.StopIfGoingOnBatteries;
                    only_idle = [bool]$settings.RunOnlyIfIdle;
                    start_when_available = [bool]$settings.StartWhenAvailable }
            }
        }
        'register' {
            $null = Register-Task $service $folder $request $sid
            $response = @{ registered = $true }
        }
        'launch' {
            if ([bool]$request.configuration.login) { throw 'business task cannot have a login trigger' }
            $task = Register-Task $service $folder $request $sid
            $instance = $task.Run($null)
            $response = @{ submitted = $true; guid = [string]$instance.InstanceGuid }
        }
        'run' {
            $task = Get-Task $folder $request.owner
            if ($null -eq $task) { throw 'task not registered' }
            $instance = $task.Run($null)
            $response = @{ submitted = $true; guid = [string]$instance.InstanceGuid }
        }
        'stop' {
            $task = Get-Task $folder $request.owner
            if ($null -ne $task) { $task.Stop(0) }
            $response = @{ stopped = $true }
        }
        'collect' {
            $status = Task-Status (Get-Task $folder $request.owner)
            $collected = -not $status.registered
            if ($status.registered -and $status.has_run -and $status.state -in @(1, 3) -and $status.instances.Count -eq 0) {
                $folder.DeleteTask([string]$request.owner, 0)
                $collected = $true
            }
            $response = @{ collected = $collected }
        }
        'delete' {
            # Removing a service definition never stops its live instance.
            $task = Get-Task $folder $request.owner
            if ($null -ne $task) { $folder.DeleteTask([string]$request.owner, 0) }
            $response = @{ deleted = $true }
        }
        'enable' {
            $task = Get-Task $folder $request.owner
            if ($null -eq $task) { throw 'task not registered' }
            $task.Enabled = [bool]$request.enabled
            $response = @{ enabled = [bool]$request.enabled }
        }
        default { throw 'invalid operation' }
    }
    $response.ok = $true
    [Console]::Out.WriteLine(($response | ConvertTo-Json -Compress -Depth 8))
    exit 0
}
catch {
    $response = @{ ok = $false; hresult = (Error-Code $_.Exception) }
    [Console]::Out.WriteLine(($response | ConvertTo-Json -Compress))
    exit 1
}
