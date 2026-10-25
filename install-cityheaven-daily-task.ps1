param([switch]$Apply)
$ErrorActionPreference = 'Stop'
$TaskName = 'TantanManagementOS-CityHeavenDaily-0810'
$SourceTaskName = 'TantanManagementOS-CTIDaily-0530'
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
try {
  $AllTasks = @(Get-ScheduledTask -ErrorAction Stop)
} catch {
  throw 'TASK_PERMISSION_REQUIRED'
}
$SourceMatches = @($AllTasks | Where-Object { $_.TaskName -eq $SourceTaskName })
if ($SourceMatches.Count -ne 1) { throw 'CTI_SOURCE_TASK_NOT_EXACTLY_ONE' }
$SourceTask = $SourceMatches[0]
$TargetMatches = @($AllTasks | Where-Object { $_.TaskName -eq $TaskName })
if ($TargetMatches.Count -gt 1) { throw 'CITYHEAVEN_TASK_DUPLICATE' }
$Task = if ($TargetMatches.Count -eq 1) { $TargetMatches[0] } else { $null }
$TaskPreexisting = $null -ne $Task
if (-not $Task -and -not $Apply) { throw 'TASK_NOT_FOUND' }
$Config = Get-Content -Raw -LiteralPath (Join-Path $Root 'local\config.json') | ConvertFrom-Json
$Python = [string]$Config.python_executable
$Pythonw = Join-Path (Split-Path -Parent $Python) 'pythonw.exe'
if (-not (Test-Path -LiteralPath $Pythonw)) { $Pythonw = $Python }
$Launcher = Join-Path $Root 'run-cityheaven-daily-metrics.py'
$ExpectedArguments = ('"{0}" --write-airtable' -f $Launcher)
$Action = New-ScheduledTaskAction -Execute $Pythonw -Argument $ExpectedArguments -WorkingDirectory $Root
$Trigger = New-ScheduledTaskTrigger -Daily -At '11:30'
$CreatedCount = 0
$UpdatedCount = 0
if (-not $Task -and $Apply) {
  try {
    Register-ScheduledTask -TaskName $TaskName -TaskPath '\' -Action $Action -Trigger $Trigger -Settings $SourceTask.Settings -Principal $SourceTask.Principal -Description 'CityHeaven Daily READ-only source fetch and existing Business Metrics writer at 11:30 JST.' | Out-Null
  } catch {
    throw 'TASK_PERMISSION_REQUIRED'
  }
  $CreatedCount = 1
}
if ($Task -and $Apply) {
  $TriggerTime = ([datetime]$Task.Triggers[0].StartBoundary).ToString('HH:mm')
  $Mismatch = (
    $Task.Actions.Count -ne 1 -or $Task.Triggers.Count -ne 1 -or
    $Task.Actions[0].Execute -ne $Pythonw -or $Task.Actions[0].Arguments -ne $ExpectedArguments -or
    $Task.Actions[0].WorkingDirectory -ne $Root -or $TriggerTime -ne '11:30' -or
    $Task.Principal.UserId -ne $SourceTask.Principal.UserId -or
    [string]$Task.Principal.LogonType -ne [string]$SourceTask.Principal.LogonType -or
    [string]$Task.Principal.RunLevel -ne [string]$SourceTask.Principal.RunLevel -or
    [string]$Task.Settings.MultipleInstances -ne [string]$SourceTask.Settings.MultipleInstances -or
    [bool]$Task.Settings.Hidden -ne [bool]$SourceTask.Settings.Hidden -or
    [bool]$Task.Settings.StartWhenAvailable -ne [bool]$SourceTask.Settings.StartWhenAvailable -or
    [bool]$Task.Settings.DisallowStartIfOnBatteries -ne [bool]$SourceTask.Settings.DisallowStartIfOnBatteries -or
    [bool]$Task.Settings.StopIfGoingOnBatteries -ne [bool]$SourceTask.Settings.StopIfGoingOnBatteries -or
    [bool]$Task.Settings.RunOnlyIfNetworkAvailable -ne [bool]$SourceTask.Settings.RunOnlyIfNetworkAvailable -or
    [bool]$Task.Settings.WakeToRun -ne [bool]$SourceTask.Settings.WakeToRun -or
    [bool]$Task.Settings.AllowHardTerminate -ne [bool]$SourceTask.Settings.AllowHardTerminate -or
    [string]$Task.Settings.ExecutionTimeLimit -ne [string]$SourceTask.Settings.ExecutionTimeLimit -or
    [int]$Task.Settings.Priority -ne [int]$SourceTask.Settings.Priority
  )
  if ($Mismatch) {
    try {
      Set-ScheduledTask -TaskName $Task.TaskName -TaskPath $Task.TaskPath -Action $Action -Trigger $Trigger -Settings $SourceTask.Settings -Principal $SourceTask.Principal | Out-Null
    } catch {
      throw 'TASK_PERMISSION_REQUIRED'
    }
    $UpdatedCount = 1
  }
  if ($Task.State -eq 'Disabled') {
    try { Enable-ScheduledTask -InputObject $Task | Out-Null } catch { throw 'TASK_PERMISSION_REQUIRED' }
    $UpdatedCount = 1
  }
}
try {
  $Task = Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop
  $Info = Get-ScheduledTaskInfo -TaskName $TaskName -ErrorAction Stop
} catch {
  throw 'TASK_PERMISSION_REQUIRED'
}
[ordered]@{
  task_name=$TaskName; task_preexisting=$TaskPreexisting; task_created_count=$CreatedCount; task_updated_count=$UpdatedCount
  exists=$true; enabled=($Task.State -ne 'Disabled'); state=[string]$Task.State
  trigger=([datetime]$Task.Triggers[0].StartBoundary).ToString('HH:mm'); next_run=$Info.NextRunTime.ToString('o')
  execute=$Task.Actions[0].Execute; arguments=$Task.Actions[0].Arguments; working_directory=$Task.Actions[0].WorkingDirectory
  principal=$Task.Principal.UserId; logon_type=[string]$Task.Principal.LogonType; run_level=[string]$Task.Principal.RunLevel
  hidden=[bool]$Task.Settings.Hidden; multiple_instances=[string]$Task.Settings.MultipleInstances
  source_task=$SourceTaskName; manual_run_count=0
} | ConvertTo-Json -Compress
