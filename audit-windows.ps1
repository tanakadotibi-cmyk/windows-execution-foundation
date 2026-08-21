param([Parameter(Mandatory=$true)][string]$ReceiptPath)

$ErrorActionPreference = 'Stop'
function Format-OptionalTime($Value) {
  if ($null -eq $Value) { return $null }
  return ([DateTime]$Value).ToString('o')
}
$TaskNames = @(
  'ExecutionFoundation-Worker','ExecutionFoundation-Watchdog',
  'GHBlog-SessionHost','GHBlog-Preflight-1430','GHBlog-Publish-1445','GHBlog-VerifyRetry-1450',
  'GHBlog-Measure-24h','GHBlog-Measure-72h','GHBlog-Measure-7d'
)
$Tasks = foreach($Name in $TaskNames){
  $Task = Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue
  if($Task){
    $Info = Get-ScheduledTaskInfo -TaskName $Name
    [ordered]@{
      task_name = $Name
      state = [string]$Task.State
      principal = $Task.Principal.UserId
      logon_type = [string]$Task.Principal.LogonType
      run_level = [string]$Task.Principal.RunLevel
      start_when_available = $Task.Settings.StartWhenAvailable
      multiple_instances = [string]$Task.Settings.MultipleInstances
      restart_count = $Task.Settings.RestartCount
      restart_interval = [string]$Task.Settings.RestartInterval
      wake_to_run = $Task.Settings.WakeToRun
      last_run_time = Format-OptionalTime $Info.LastRunTime
      last_task_result = $Info.LastTaskResult
      next_run_time = Format-OptionalTime $Info.NextRunTime
    }
  }
}
$TimeService = Get-Service W32Time
$Receipt = [ordered]@{
  status = 'SUCCESS'
  observed_at = [DateTimeOffset]::Now.ToString('o')
  task_history_enabled = ((wevtutil.exe get-log Microsoft-Windows-TaskScheduler/Operational | Select-String 'enabled: true') -ne $null)
  w32time = [ordered]@{ status=[string]$TimeService.Status; start_type=[string]$TimeService.StartType }
  time_status = (w32tm.exe /query /status | Out-String).Trim()
  active_power_scheme = (powercfg.exe /getactivescheme | Out-String).Trim()
  sleep_settings = (powercfg.exe /query SCHEME_CURRENT SUB_SLEEP | Out-String)
  tasks = @($Tasks)
}
New-Item -ItemType Directory -Path (Split-Path -Parent $ReceiptPath) -Force | Out-Null
$Receipt | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $ReceiptPath -Encoding UTF8
$Receipt | ConvertTo-Json -Depth 8
