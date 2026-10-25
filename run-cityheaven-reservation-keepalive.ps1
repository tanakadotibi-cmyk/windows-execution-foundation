$ErrorActionPreference = 'Stop'
$cdpPort = 9227
$repo = 'C:\Users\epuro\Documents\Codex\2026-09-18\windows-execution-foundation'
function Test-ReservationPaused {
    $stream = $null
    try {
        $marker = Join-Path $repo 'local\reservation-chat-pause.json'
        $stream = [System.IO.File]::Open($marker, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read, [System.IO.FileShare]::ReadWrite)
        $buffer = New-Object byte[] 4097
        $n = 0
        while ($n -lt $buffer.Length) {
            $got = $stream.Read($buffer, $n, $buffer.Length - $n)
            if ($got -eq 0) { break }
            $n += $got
        }
        if ($n -gt 4096) { return $true }
        $text = [System.Text.UTF8Encoding]::new($false, $true).GetString($buffer, 0, $n)
        $value = $text | ConvertFrom-Json -ErrorAction Stop
        return -not ($null -ne $value -and @($value.psobject.Properties).Count -eq 2 -and
            $value.scope -ceq 'CITYHEAVEN_RESERVATION_SCHEDULE' -and
            $value.paused -is [bool] -and $value.paused -eq $false)
    } catch { return $true }
    finally { if ($null -ne $stream) { $stream.Dispose() } }
}
$reservationPaused = Test-ReservationPaused
$configuration = Get-Content -LiteralPath (Join-Path $repo 'local\config.json') -Raw | ConvertFrom-Json
if ($configuration.cityheaven_daily.browser -cne 'RESERVATION_CHROME_REUSE') { Write-Output 'DAILY_ROUTE_NOT_ENABLED'; exit 0 }
if ([int]$configuration.cityheaven_reservation.cdp_port -ne $cdpPort) { throw 'DAILY_PORT_MISMATCH' }
$listener = @(Get-NetTCPConnection -LocalPort $cdpPort -State Listen -ErrorAction SilentlyContinue)
if ($listener.Count -ne 1) { throw 'DAILY_BROWSER_UNAVAILABLE' }
$browserProcess = Get-CimInstance Win32_Process -Filter ('ProcessId=' + $listener[0].OwningProcess)
$expectedProfile = [string]$configuration.cityheaven_reservation.dedicated_profile_path
if ($browserProcess.Name -ne 'chrome.exe' -or $browserProcess.ExecutablePath -ine $configuration.cityheaven_reservation.chrome_executable -or $browserProcess.CommandLine -notmatch ('--user-data-dir="?' + [regex]::Escape($expectedProfile) + '"?(?:\s|$)')) { throw 'DAILY_BROWSER_IDENTITY_MISMATCH' }
$helper = Join-Path $repo 'src\windows_execution_foundation\cityheaven_daily_keepalive.js'
$runtime = Join-Path $env:LOCALAPPDATA 'WindowsExecutionFoundation\cityheaven-reservation-keepalive'
$log = Join-Path $runtime 'keepalive.jsonl'
$stateFile = Join-Path $runtime 'state.json'
$targetUrl = 'https://newmanager.cityheaven.net/C2GirlList.php?shopdir=s_apronlady'
New-Item -ItemType Directory -Path $runtime -Force | Out-Null
try {
    $targets = Invoke-RestMethod -Uri "http://127.0.0.1:$cdpPort/json/list" -TimeoutSec 3
    $hasManager = $false
    foreach ($t in $targets) {
        if ($t.type -ne 'page') { continue }
        try { if (([uri]$t.url).Host -eq 'newmanager.cityheaven.net') { $hasManager = $true; break } } catch {}
    }
    if (-not $hasManager) {
        $escaped = [uri]::EscapeDataString($targetUrl)
        Invoke-RestMethod -Method Put -Uri "http://127.0.0.1:$cdpPort/json/new?$escaped" -TimeoutSec 5 | Out-Null
        Start-Sleep -Seconds 2
    }
    & node $helper --cdp-port $cdpPort --output $log | Out-Null
    $helperExit = $LASTEXITCODE
    $record = Get-Content -LiteralPath $log -Tail 1 | ConvertFrom-Json
    $current = if ($record.auth_class) { [string]$record.auth_class } else { 'UNKNOWN' }
} catch {
    $helperExit = 20
    $current = 'UNKNOWN'
}
$previous = $null
if (Test-Path -LiteralPath $stateFile) {
    try { $previous = (Get-Content -LiteralPath $stateFile -Raw | ConvertFrom-Json).auth_class } catch {}
}
[ordered]@{
    observed_at = (Get-Date).ToString('o')
    auth_class = $current
    helper_exit = $helperExit
    scope = 'DAILY_CMS_AUTH_READ_ONLY'
    reservation_updates_paused = $reservationPaused
} | ConvertTo-Json -Compress | Set-Content -LiteralPath $stateFile -Encoding utf8
if ($current -ne 'AUTHENTICATED' -and $previous -ne $current) {
    $webhook = [Environment]::GetEnvironmentVariable('TANTAN_SLACK_WEBHOOK_URL','User')
    if ($webhook) {
        $text = "CityHeaven daily CMS auth needs attention. State=$current. Re-login to newmanager on the dedicated M720S Chrome profile."
        try { Invoke-RestMethod -Method Post -Uri $webhook -ContentType 'application/json' -Body (@{text=$text} | ConvertTo-Json -Compress) | Out-Null } catch {}
    }
}
if ($current -eq 'AUTHENTICATED') { exit 0 }
exit 20
