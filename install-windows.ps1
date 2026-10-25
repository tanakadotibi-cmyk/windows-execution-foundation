param(
  [Parameter(Mandatory=$true)][string]$ConfigPath,
  [string]$ReceiptPath,
  [switch]$SkipPowerChanges,
  [switch]$Uninstall
)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$StartedAt = [DateTimeOffset]::Now
if (-not $ReceiptPath) { $ReceiptPath = Join-Path $Root 'outputs\windows-install-receipt.json' }
$ConfigPath = (Resolve-Path -LiteralPath $ConfigPath).Path
$Runner = Join-Path $Root 'run-foundation.ps1'
$Config = Get-Content -LiteralPath $ConfigPath -Raw -Encoding UTF8 | ConvertFrom-Json
$Pythonw = [string]$Config.python_executable -replace 'python\.exe$','pythonw.exe'
if (-not (Test-Path -LiteralPath $Pythonw)) { throw 'PYTHONW_REQUIRED' }
$WorkerHidden = Join-Path $Root 'run-worker-hidden.pyw'
$WatchdogHidden = Join-Path $Root 'run-watchdog-hidden.pyw'
$WorkerName = 'ExecutionFoundation-Worker'
$WatchdogName = 'ExecutionFoundation-Watchdog'

if ($Uninstall) {
  Unregister-ScheduledTask -TaskName $WorkerName -Confirm:$false -ErrorAction SilentlyContinue
  Unregister-ScheduledTask -TaskName $WatchdogName -Confirm:$false -ErrorAction SilentlyContinue
  return
}

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principalCheck = New-Object Security.Principal.WindowsPrincipal($identity)
if (-not $principalCheck.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
  throw 'ADMINISTRATOR_REQUIRED'
}

wevtutil.exe set-log Microsoft-Windows-TaskScheduler/Operational /enabled:true
Set-Service -Name W32Time -StartupType Automatic
Start-Service -Name W32Time
w32tm.exe /config /syncfromflags:manual /manualpeerlist:"time.windows.com,0x9 time.google.com,0x9" /update
w32tm.exe /resync /force

if (-not $SkipPowerChanges) {
  powercfg.exe /change standby-timeout-ac 0
  powercfg.exe /change standby-timeout-dc 0
  powercfg.exe /change hibernate-timeout-ac 0
  powercfg.exe /change hibernate-timeout-dc 0
  powercfg.exe /setacvalueindex SCHEME_CURRENT SUB_SLEEP HYBRIDSLEEP 0
  powercfg.exe /setdcvalueindex SCHEME_CURRENT SUB_SLEEP HYBRIDSLEEP 0
  powercfg.exe /setacvalueindex SCHEME_CURRENT SUB_SLEEP RTCWAKE 1
  powercfg.exe /setdcvalueindex SCHEME_CURRENT SUB_SLEEP RTCWAKE 1
  powercfg.exe /setactive SCHEME_CURRENT
}

$PowerShell = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"
$WorkerAction = New-ScheduledTaskAction -Execute $Pythonw -Argument ('"{0}"' -f $WorkerHidden) -WorkingDirectory $Root
$WatchdogAction = New-ScheduledTaskAction -Execute $Pythonw -Argument ('"{0}"' -f $WatchdogHidden) -WorkingDirectory $Root
$WorkerSettings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable -RestartCount 5 -RestartInterval (New-TimeSpan -Minutes 5) -ExecutionTimeLimit (New-TimeSpan -Minutes 20) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -WakeToRun
$WatchdogSettings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 5) -ExecutionTimeLimit (New-TimeSpan -Minutes 3) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -WakeToRun
$CurrentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$WorkerPrincipal = New-ScheduledTaskPrincipal -UserId $CurrentUser -LogonType Interactive -RunLevel Limited
$SystemPrincipal = New-ScheduledTaskPrincipal -UserId $CurrentUser -LogonType Interactive -RunLevel Limited
$WorkerTriggers = @((New-ScheduledTaskTrigger -AtLogOn -User $CurrentUser), (New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 5)))
$WatchdogTriggers = @((New-ScheduledTaskTrigger -AtLogOn -User $CurrentUser), (New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(3) -RepetitionInterval (New-TimeSpan -Minutes 5)))
Register-ScheduledTask -TaskName $WorkerName -Action $WorkerAction -Trigger $WorkerTriggers -Settings $WorkerSettings -Principal $WorkerPrincipal -Description 'Remote-independent durable UI worker. Interactive user principal with automatic logon start and five-minute fallback.' -Force | Out-Null
Register-ScheduledTask -TaskName $WatchdogName -Action $WatchdogAction -Trigger $WatchdogTriggers -Settings $WatchdogSettings -Principal $SystemPrincipal -Description 'Independent heartbeat watchdog and stale spool recovery.' -Force | Out-Null
Start-ScheduledTask -TaskName $WorkerName
Start-ScheduledTask -TaskName $WatchdogName
$Tasks = Get-ScheduledTask -TaskName $WorkerName,$WatchdogName | Select-Object TaskName,State,@{n='Principal';e={$_.Principal.UserId}},@{n='LogonType';e={$_.Principal.LogonType}},@{n='RunLevel';e={$_.Principal.RunLevel}},@{n='StartWhenAvailable';e={$_.Settings.StartWhenAvailable}},@{n='RestartCount';e={$_.Settings.RestartCount}}
$Receipt = [ordered]@{
  status = 'SUCCESS'
  started_at = $StartedAt.ToString('o')
  completed_at = [DateTimeOffset]::Now.ToString('o')
  task_history_enabled = ((wevtutil.exe get-log Microsoft-Windows-TaskScheduler/Operational | Select-String 'enabled: true') -ne $null)
  w32time = (Get-Service W32Time | Select-Object Name,Status,StartType)
  tasks = @($Tasks)
  power_scheme = (powercfg.exe /getactivescheme | Out-String).Trim()
}
New-Item -ItemType Directory -Path (Split-Path -Parent $ReceiptPath) -Force | Out-Null
$Receipt | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $ReceiptPath -Encoding UTF8
$Tasks
