# 導入とロールバック

対象マシンのリポジトリは `C:\Users\epuro\Documents\Codex\2026-09-18\windows-execution-foundation` です。タスク名は現状のまま（日次は `\TantanManagementOS-CityHeavenDaily-0810`、時刻は 11:30）で、中身だけ logged ラッパーに差し替えます。同名の第二タスクは作りません。

`install-cityheaven-daily-task.ps1` は実行しないでください。`-Apply` は引数を元の `run-cityheaven-daily-metrics.py` に戻します。

ブラウザはマシンに残しません。ロックも参照カウントも使いません。11:30 の日次と 14:30 のリトライは重ならない同じジョブで、どちらもポート 9227 と `ReservationRuntimeChromeProfile` を使います。止まっていれば同じフラグで起動し、すでに起動済みならプロファイルを確認して再利用します。成功・失敗・403 を問わず、その実行の `finally` で閉じます。先に CDP の `Browser.close`、まだ残っていればその `--user-data-dir` の `chrome.exe` だけを止めます。プロファイルディレクトリは残します。

起動タスクは `\TantanX-CityReservationChrome9227-Ensure-V2` です。中身は PowerShell の `Start-Process`（`UseShellExecute`）で、起動したらタスク自身は終わります。Chrome を抱えたままにはしません。実行時間制限 `PT1M` はランチャーだけにかかります。稼働中の `\TantanX-CityReservationChrome9227-Restart-Once` へは `schtasks /Create` しません。

起動時に前回のタブは復元しません。`--restore-last-session` は使いません。開始 URL は `about:blank` で、`--disable-session-crashed-bubble` と `--hide-crash-restore-bubble` を付けます。予約プロファイルの Chrome が止まっているときだけ、起動の直前に `Default\Preferences` を編集します。`session.restore_on_startup` は 4（指定した URL を開く。5 は新しいタブページなので使いません）、`session.startup_urls` は `about:blank` だけ、`profile.exit_type` は `Normal`、`profile.exited_cleanly` は true です。既存の Preferences は、同じフォルダに `Preferences.before-norestore` がまだ無いときだけそこへコピーします。バックアップは上書きしません。Chrome が動いている間は編集しません。この編集はページを開きません。すでに起動している Chrome はタブを捨てて起動し直さず、プロファイルを確認して再利用し、その実行の終わりに閉じます。次に止まっている状態から起動するときが `about:blank` だけです。

## 1. 新しいファイルを本番ツリーへコピー

`$Src` はこの成果物のルート（`run-cityheaven-daily-metrics-logged.py` と `SHA256SUMS.txt` があるディレクトリ）です。`reservation_pause.py` はコピーしません。先にハッシュを確認します。

```powershell
$ErrorActionPreference = 'Stop'
$Src = 'C:\path\to\cityheaven-daily-v2'
Set-Location $Src
foreach ($line in Get-Content -LiteralPath .\SHA256SUMS.txt) {
  if ($line -notmatch '^([0-9a-f]{64})  (.+)$') { continue }
  $hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $Matches[2]).Hash.ToLower()
  if ($hash -ne $Matches[1]) { throw "HASH_MISMATCH $($Matches[2])" }
}
'SHA256_OK'
```

```powershell
$ErrorActionPreference = 'Stop'
$Src = 'C:\path\to\cityheaven-daily-v2'
$Root = 'C:\Users\epuro\Documents\Codex\2026-09-18\windows-execution-foundation'
$files = @(
  'prepare-reservation-chrome-norestore.py',
  'run-cityheaven-daily-metrics-logged.py',
  'run-cityheaven-daily-metrics-v2.py',
  'run-cityheaven-update-count-retry-logged.py',
  'run-cityheaven-update-count-retry-v2.py',
  'src\windows_execution_foundation\cityheaven_logged_launch_v2.py',
  'src\windows_execution_foundation\cityheaven_daily_metrics_v2.py',
  'src\windows_execution_foundation\cityheaven_update_count_retry_v2.py',
  'src\windows_execution_foundation\cityheaven_reservation_chrome_ensure_v2.py',
  'src\windows_execution_foundation\cityheaven_reservation_runtime_ensure_v2.js',
  'tasks\TantanManagementOS-CityHeavenDaily-1130-v2.xml',
  'tasks\TantanManagementOS-CityHeavenUpdateCountRetry-1430-v2.xml',
  'tasks\TantanX-CityReservationChrome9227-Restart-Once-v2.xml',
  'tests\fixtures\chrome9227_cmdline_after_restart.txt',
  'tests\fixtures\chrome9227_cmdline_before_restart.txt',
  'tests\fixtures\receipts\20261020_FAILED_1130_moved_aside.json',
  'tests\fixtures\receipts\20261020.json',
  'tests\test_cityheaven_chrome_norestore_v2.py',
  'tests\test_cityheaven_reservation_chrome_ensure_v2.py',
  'tests\test_cityheaven_daily_metrics_v2.py',
  'tests\test_cityheaven_update_count_retry_v2.py',
  'tests\test_cityheaven_logged_launcher_v2.py',
  'tests\test_cityheaven_runtime_ensure_v2_js.py',
  'tests\test_cityheaven_reservation_runtime_ensure_v2.js',
  'RUN.md'
)
foreach ($rel in $files) {
  $dest = Join-Path $Root $rel
  New-Item -ItemType Directory -Force -Path (Split-Path -Parent $dest) | Out-Null
  Copy-Item -LiteralPath (Join-Path $Src $rel) -Destination $dest -Force
}
```

## 2. 作業前のタスク定義を控える

日次と件数リトライだけを控えます。稼働中の Chrome ワンショット `\TantanX-CityReservationChrome9227-Restart-Once` はエクスポートも再登録もしません。

```powershell
$ErrorActionPreference = 'Stop'
$Root = 'C:\Users\epuro\Documents\Codex\2026-09-18\windows-execution-foundation'
$Backup = Join-Path $Root 'local\backup-20261021-cdp-mismatch-v2'
New-Item -ItemType Directory -Force -Path $Backup | Out-Null
foreach ($name in @(
  'TantanManagementOS-CityHeavenDaily-0810',
  'TantanManagementOS-CityHeavenUpdateCountRetry-1430'
)) {
  schtasks /Query /TN "\$name" /XML | Out-File -FilePath (Join-Path $Backup "$name.live.xml") -Encoding unicode
}
```

## 3. タスクを登録する（これで導入完了）

11:30 の日次、14:30 の件数リトライ、および Chrome 用の新しいタスク `\TantanX-CityReservationChrome9227-Ensure-V2` を登録します。XML ファイル名は `Restart-Once-v2.xml` のままですが、中の URI は Ensure-V2 です。同梱 XML は無効（トリガー無し）なので、この登録だけでは Chrome は起動しません。ジョブが起動するときは同じタスク名で `Start-Process` のワンショットを作り、起動コマンドが終わったあと無効に戻します。タスクは Chrome を子プロセスとして持ち続けません。

`\TantanX-CityReservationChrome9227-Restart-Once` へ `/Create` しないでください。

```powershell
$ErrorActionPreference = 'Stop'
$Root = 'C:\Users\epuro\Documents\Codex\2026-09-18\windows-execution-foundation'
schtasks /Create /TN "\TantanManagementOS-CityHeavenDaily-0810" /XML "$Root\tasks\TantanManagementOS-CityHeavenDaily-1130-v2.xml" /F
schtasks /Create /TN "\TantanManagementOS-CityHeavenUpdateCountRetry-1430" /XML "$Root\tasks\TantanManagementOS-CityHeavenUpdateCountRetry-1430-v2.xml" /F
schtasks /Create /TN "\TantanX-CityReservationChrome9227-Ensure-V2" /XML "$Root\tasks\TantanX-CityReservationChrome9227-Restart-Once-v2.xml" /F
schtasks /Query /TN "\TantanManagementOS-CityHeavenDaily-0810" /V /FO LIST
schtasks /Query /TN "\TantanManagementOS-CityHeavenUpdateCountRetry-1430" /V /FO LIST
schtasks /Query /TN "\TantanX-CityReservationChrome9227-Ensure-V2" /V /FO LIST
```

日次の実行ファイルが `run-cityheaven-daily-metrics-logged.py`、リトライが `run-cityheaven-update-count-retry-logged.py`、開始時刻が 11:30 と 14:30 であることを確認します。Chrome タスクの XML に `<URI>\TantanX-CityReservationChrome9227-Ensure-V2</URI>`、`<ExecutionTimeLimit>PT1M</ExecutionTimeLimit>`、`UseShellExecute` があることを確認します。`<Command>` は `powershell.exe` です。引数に `--restore-last-session` が無く、`about:blank` があることも確認します。

## 4. CityHeaven に触らない確認

止まっていれば予約プロファイルの Chrome を一度起動し、すでに居ればプロファイル確認のあと再利用します。起動する直前には、上の Preferences 編集（バックアップ付き）をします。起動済みなら Preferences は触りません。確認の終わりにその Chrome を閉じます。他の Chrome は止めません。CityHeaven へは行きません。終了後に 9227 のブラウザが残っていないことを確認します。

Chrome を閉じた状態で Preferences だけ先に直す場合:

```powershell
& $Python .\prepare-reservation-chrome-norestore.py
```

予約プロファイルの `chrome.exe` が動いていると何も書かずに終わります。`Default\Preferences.before-norestore` が既にあるときは上書きしません。

```powershell
$ErrorActionPreference = 'Stop'
$Root = 'C:\Users\epuro\Documents\Codex\2026-09-18\windows-execution-foundation'
$Python = 'C:\Users\epuro\AppData\Local\TantanManagementOS\pyenv-tasks\Scripts\python.exe'
Set-Location $Root
& $Python .\run-cityheaven-daily-metrics-v2.py --dry-run
& $Python .\run-cityheaven-update-count-retry-v2.py --dry-run
```

終了コード 0 はポート 9227 のプロファイルが一致。20 は再起動 1 回のあとでも不一致です。標準エラーの `CITYHEAVEN_ENSURE:` が判定結果です。

ログは `%LOCALAPPDATA%\WindowsExecutionFoundation\cityheaven-daily\logs\YYYYMMDD_HHMMSS.log` です。ラッパーは子プロセスの終了コードをそのまま返します。

## ロールバック

日次と件数リトライだけを同梱の元 XML に戻します。追加した Chrome タスクは削除します。稼働中の `\TantanX-CityReservationChrome9227-Restart-Once` は再登録しません。元 XML で `/Create` し直すと、そのワンショット定義が置き換わります。

```powershell
$ErrorActionPreference = 'Stop'
$Root = 'C:\Users\epuro\Documents\Codex\2026-09-18\windows-execution-foundation'
schtasks /Create /TN "\TantanManagementOS-CityHeavenDaily-0810" /XML "$Root\tasks\TantanManagementOS-CityHeavenDaily-0810.xml" /F
schtasks /Create /TN "\TantanManagementOS-CityHeavenUpdateCountRetry-1430" /XML "$Root\tasks\TantanManagementOS-CityHeavenUpdateCountRetry-1430.xml" /F
schtasks /Delete /TN "\TantanX-CityReservationChrome9227-Ensure-V2" /F
```

作業前の実体へ戻す場合は、セクション 2 の `live.xml` を日次とリトライにだけ渡します。

```powershell
$Root = 'C:\Users\epuro\Documents\Codex\2026-09-18\windows-execution-foundation'
$Backup = Join-Path $Root 'local\backup-20261021-cdp-mismatch-v2'
schtasks /Create /TN "\TantanManagementOS-CityHeavenDaily-0810" /XML "$Backup\TantanManagementOS-CityHeavenDaily-0810.live.xml" /F
schtasks /Create /TN "\TantanManagementOS-CityHeavenUpdateCountRetry-1430" /XML "$Backup\TantanManagementOS-CityHeavenUpdateCountRetry-1430.live.xml" /F
schtasks /Delete /TN "\TantanX-CityReservationChrome9227-Ensure-V2" /F
```

追加ファイルを外す場合:

```powershell
$Root = 'C:\Users\epuro\Documents\Codex\2026-09-18\windows-execution-foundation'
$files = @(
  'prepare-reservation-chrome-norestore.py',
  'run-cityheaven-daily-metrics-logged.py',
  'run-cityheaven-daily-metrics-v2.py',
  'run-cityheaven-update-count-retry-logged.py',
  'run-cityheaven-update-count-retry-v2.py',
  'src\windows_execution_foundation\cityheaven_logged_launch_v2.py',
  'src\windows_execution_foundation\cityheaven_daily_metrics_v2.py',
  'src\windows_execution_foundation\cityheaven_update_count_retry_v2.py',
  'src\windows_execution_foundation\cityheaven_reservation_chrome_ensure_v2.py',
  'src\windows_execution_foundation\cityheaven_reservation_runtime_ensure_v2.js',
  'tasks\TantanManagementOS-CityHeavenDaily-1130-v2.xml',
  'tasks\TantanManagementOS-CityHeavenUpdateCountRetry-1430-v2.xml',
  'tasks\TantanX-CityReservationChrome9227-Restart-Once-v2.xml',
  'tests\fixtures\chrome9227_cmdline_after_restart.txt',
  'tests\fixtures\chrome9227_cmdline_before_restart.txt',
  'tests\fixtures\receipts\20261020_FAILED_1130_moved_aside.json',
  'tests\fixtures\receipts\20261020.json',
  'tests\test_cityheaven_chrome_norestore_v2.py',
  'tests\test_cityheaven_reservation_chrome_ensure_v2.py',
  'tests\test_cityheaven_daily_metrics_v2.py',
  'tests\test_cityheaven_update_count_retry_v2.py',
  'tests\test_cityheaven_logged_launcher_v2.py',
  'tests\test_cityheaven_runtime_ensure_v2_js.py',
  'tests\test_cityheaven_reservation_runtime_ensure_v2.js'
)
foreach ($rel in $files) {
  Remove-Item -LiteralPath (Join-Path $Root $rel) -Force -ErrorAction SilentlyContinue
}
```

レシートと `logs` は消しません。
