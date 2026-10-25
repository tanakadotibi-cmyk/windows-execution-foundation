[CmdletBinding()]
param(
    [datetime]$StartAt = (Get-Date).AddMinutes(2),
    [string]$EvidencePath = "C:\Users\epuro\Documents\Codex\2026-09-18\windows-execution-foundation\local\rank-observer-task-registration-v01-20260926.json"
)

$ErrorActionPreference = 'Stop'
$taskName = 'RankObserver-BusinessMetrics-M720S-v01'
$root = 'C:\Users\epuro\Documents\Codex\2026-09-18\windows-execution-foundation'
$pythonw = 'C:\Users\epuro\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\pythonw.exe'
$runner = Join-Path $root 'run-rank-observer-metrics.py'
$syncEvidence = Join-Path $root 'local\rank-observer-airtable-sync-v01-20260926.json'

if (-not (Test-Path -LiteralPath $pythonw -PathType Leaf)) { throw 'PYTHONW_MISSING' }
if (-not (Test-Path -LiteralPath $runner -PathType Leaf)) { throw 'RUNNER_MISSING' }
if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) { throw 'TASK_ALREADY_EXISTS' }

$future = $StartAt
if ($future -le (Get-Date).AddSeconds(45)) { throw 'START_TIME_NOT_FUTURE' }
$argument = '"' + $runner + '" --write-airtable --acceptance-replay --evidence "' + $syncEvidence + '"'
$action = New-ScheduledTaskAction -Execute $pythonw -Argument $argument -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -Daily -At $future
$settings = New-ScheduledTaskSettingsSet -Hidden -ExecutionTimeLimit (New-TimeSpan -Minutes 5) -MultipleInstances IgnoreNew -StartWhenAvailable
$principal = New-ScheduledTaskPrincipal -UserId ([Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType S4U -RunLevel Limited
$definition = New-ScheduledTask -Action $action -Trigger $trigger -Settings $settings -Principal $principal
$registered = Register-ScheduledTask -TaskName $taskName -InputObject $definition
$xml = Export-ScheduledTask -TaskName $taskName
$bytes = [Text.Encoding]::Unicode.GetBytes($xml)
$sha = [Convert]::ToHexString([Security.Cryptography.SHA256]::HashData($bytes)).ToLowerInvariant()
$info = Get-ScheduledTaskInfo -TaskName $taskName
$evidence = [ordered]@{
    schema = 'rank_observer_task_registration/1.0'
    status = 'PASS'
    task_name = $taskName
    registered_at = (Get-Date).ToString('o')
    trigger_start = $future.ToString('o')
    next_run_time = $info.NextRunTime.ToString('o')
    last_run_time = $info.LastRunTime.ToString('o')
    last_task_result = $info.LastTaskResult
    principal_user = $principal.UserId
    logon_type = [string]$principal.LogonType
    run_level = [string]$principal.RunLevel
    hidden = $settings.Hidden
    execution_time_limit_seconds = [int]$settings.ExecutionTimeLimit.TotalSeconds
    multiple_instances = [string]$settings.MultipleInstances
    action_execute = $pythonw
    action_arguments = $argument
    working_directory = $root
    task_xml_sha256 = $sha
    manual_start_used = $false
}
$parent = Split-Path -Parent $EvidencePath
New-Item -ItemType Directory -Path $parent -Force | Out-Null
$temporary = $EvidencePath + '.tmp'
$evidence | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $temporary -Encoding UTF8
Move-Item -LiteralPath $temporary -Destination $EvidencePath -Force
