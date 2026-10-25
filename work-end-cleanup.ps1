param([switch]$Apply)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$resultDir = "C:\Users\epuro\AppData\Local\WindowsExecutionFoundation\local-exec-v01"
$historyDir = Join-Path $resultDir "work-end-cleanup-history"
$resultPath = Join-Path $resultDir "work-end-cleanup-result.json"
New-Item -ItemType Directory -Force -Path $historyDir | Out-Null

function Get-MemoryState {
    $os = Get-CimInstance Win32_OperatingSystem
    [ordered]@{
        total_mb = [math]::Round($os.TotalVisibleMemorySize / 1024, 0)
        free_mb = [math]::Round($os.FreePhysicalMemory / 1024, 0)
        used_mb = [math]::Round(($os.TotalVisibleMemorySize - $os.FreePhysicalMemory) / 1024, 0)
    }
}

function Get-TargetProcesses {
    $cim = @(Get-CimInstance Win32_Process)
    $live = @{}
    foreach ($p in @(Get-Process -ErrorAction SilentlyContinue)) { $live[$p.Id] = $p }
    $rows = @()
    foreach ($ci in $cim) {
        $name = [string]$ci.Name
        $cmd = [string]$ci.CommandLine
        $direct = ($name -in @("opera.exe","ChatGPT.exe")) -or ($name -like "codex*.exe")
        $codexChild = ($name -in @("node.exe","node_repl.exe","cmd.exe","conhost.exe")) -and
            ($cmd -match '(?i)(\\OpenAI\\Codex\\|\\.codex\\|codex-command-runner|codex-primary-runtime)')
        if (-not ($direct -or $codexChild)) { continue }
        if ($ci.ProcessId -eq $PID) { continue }
        $p = $live[[int]$ci.ProcessId]
        $mem = if ($null -ne $p) { [math]::Round($p.WorkingSet64 / 1MB, 1) } else { 0 }
        $rows += [pscustomobject]@{
            name = $name
            pid = [int]$ci.ProcessId
            ppid = [int]$ci.ParentProcessId
            memory_mb = $mem
            reason = if ($direct) { "interactive_ai_app" } else { "codex_child_runtime" }
        }
    }
    return @($rows)
}

function Get-TargetMemoryMb {
    param($Targets)
    if ($null -eq $Targets -or @($Targets).Count -eq 0) { return 0 }
    $sum = (@($Targets) | Measure-Object -Property memory_mb -Sum).Sum
    if ($null -eq $sum) { return 0 }
    return [math]::Round([double]$sum, 1)
}

function Get-ProtectedState {
    $readyPath = "C:\Users\epuro\AppData\Local\TantanManagementOS\CodexBridge\runtime\watcher-ready.json"
    $ready = $null
    try {
        if (Test-Path -LiteralPath $readyPath) { $ready = Get-Content -LiteralPath $readyPath -Raw | ConvertFrom-Json }
    } catch {}
    $bridgePid = if ($null -ne $ready -and $ready.pid) { [int]$ready.pid } else { 0 }
    $bridgeProc = if ($bridgePid -gt 0) { Get-Process -Id $bridgePid -ErrorAction SilentlyContinue } else { $null }
    $bridgeTask = Get-ScheduledTask -TaskName "TantanOS-CodexBridge-Highest" -ErrorAction SilentlyContinue
    $workerTask = Get-ScheduledTask -TaskName "ExecutionFoundation-Worker" -ErrorAction SilentlyContinue
    [ordered]@{
        bridge_pid = $bridgePid
        bridge_process_count = if ($null -ne $bridgeProc) { 1 } else { 0 }
        bridge_ready_status = if ($null -ne $ready) { [string]$ready.status } else { "MISSING" }
        bridge_task_state = if ($null -ne $bridgeTask) { [string]$bridgeTask.State } else { "MISSING" }
        worker_task_state = if ($null -ne $workerTask) { [string]$workerTask.State } else { "MISSING" }
        bridge_ok = ($null -ne $bridgeProc -and $null -ne $ready -and [string]$ready.status -eq "READY" -and $null -ne $bridgeTask -and [string]$bridgeTask.State -in @("Running","Ready"))
        worker_ok = ($null -ne $workerTask -and [string]$workerTask.State -in @("Running","Ready"))
    }
}

$started = [DateTimeOffset]::Now
$beforeTargets = @(Get-TargetProcesses)
$beforeMemory = Get-MemoryState
$beforeProtected = Get-ProtectedState
$closedWindowPids = @()
$forcedPids = @()

if ($Apply) {
    foreach ($p in @(Get-Process -Name "opera","ChatGPT" -ErrorAction SilentlyContinue |
        Where-Object { $_.MainWindowHandle -ne 0 })) {
        try {
            if ($p.CloseMainWindow()) { $closedWindowPids += $p.Id }
        } catch {}
    }
    Start-Sleep -Seconds 4

    $remaining = @(Get-TargetProcesses)
    foreach ($row in ($remaining | Sort-Object pid -Unique)) {
        try {
            Stop-Process -Id $row.pid -Force -ErrorAction Stop
            $forcedPids += $row.pid
        } catch {}
    }
    Start-Sleep -Seconds 4
}

$afterTargets = @(Get-TargetProcesses)
$afterMemory = Get-MemoryState
$afterProtected = Get-ProtectedState
$reclaimed = [math]::Round($afterMemory.free_mb - $beforeMemory.free_mb, 0)
$success = (-not $Apply) -or (
    $afterTargets.Count -eq 0 -and
    $afterProtected.bridge_ok -and
    $afterProtected.worker_ok
)

$result = [ordered]@{
    schema_version = "1.0"
    rule = "WORK_END_CLEANUP"
    mode = if ($Apply) { "APPLY" } else { "AUDIT" }
    status = if ($success) { if ($Apply) { "SUCCESS" } else { "AUDIT_ONLY" } } else { "POSTCONDITION_FAILED" }
    started_at = $started.ToString("o")
    finished_at = [DateTimeOffset]::Now.ToString("o")
    before = [ordered]@{ target_count = $beforeTargets.Count; target_memory_mb = Get-TargetMemoryMb -Targets $beforeTargets; memory = $beforeMemory; protected = $beforeProtected }
    action = [ordered]@{ graceful_window_close_pids = @($closedWindowPids); forced_stop_pids = @($forcedPids) }
    after = [ordered]@{ target_count = $afterTargets.Count; target_memory_mb = Get-TargetMemoryMb -Targets $afterTargets; memory = $afterMemory; protected = $afterProtected }
    reclaimed_free_memory_mb = $reclaimed
    remaining_targets = @($afterTargets)
}
$json = $result | ConvertTo-Json -Depth 8
[IO.File]::WriteAllText($resultPath, $json + [Environment]::NewLine, [Text.UTF8Encoding]::new($false))
$stamp = (Get-Date).ToString("yyyyMMdd-HHmmss")
[IO.File]::WriteAllText((Join-Path $historyDir "work-end-cleanup-$stamp.json"), $json + [Environment]::NewLine, [Text.UTF8Encoding]::new($false))
Write-Output $json
if (-not $success) { exit 2 }
