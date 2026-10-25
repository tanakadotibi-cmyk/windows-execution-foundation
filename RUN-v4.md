# CityHeaven 日次集計 v4（Google シート）

既存の v2 / v3 のファイルは変更しません。タスク定義もこの作業では変えません。切替は店長の確認のあと、既存タスクの引数だけを v4 のランチャーに向けます。戻すときも、引数を v3 のランチャーに戻すだけです。

対象マシンのリポジトリは `C:\Users\epuro\Documents\Codex\2026-09-18\windows-execution-foundation` です。Python は次です。

```powershell
$Python = 'C:\Users\epuro\AppData\Local\TantanManagementOS\pyenv-tasks\Scripts\python.exe'
$Root = 'C:\Users\epuro\Documents\Codex\2026-09-18\windows-execution-foundation'
Set-Location $Root
```

書き先スプレッドシートは「経営数字_CTI_City_v1」（ID `1R_xQV6AYIwRRK1fW-AInwm2rXoThmOFRhNSjgeH8swI`）です。本番タブは `city_metric_detail` / `city_daily_summary` / `run_log` です。Airtable には読み書きしません。結果 JSON の `airtable_write_count` は 0 です。

認証は CurrentUser DPAPI で暗号化したサービスアカウント JSON です。既定パスは `C:\TantanJobs\secrets\sheets_writer_sa.dpapi`（ユーザー epuro）です。別の場所なら `--credential` で渡します。サービスアカウントは `<SA_EMAIL>`、scope は `https://www.googleapis.com/auth/spreadsheets` だけです。鍵の中身とトークンは標準出力・ログ・例外に出しません。pyenv-tasks の Python に google-auth 2.62.0 が入っている前提です。

## 1. 新しいファイルを置く

`$Src` は成果物 zip を展開したディレクトリです。先にハッシュを確認します。`SHA256SUMS.txt` は zip の中にあります。

```powershell
$ErrorActionPreference = 'Stop'
$Src = 'C:\path\to\cityheaven-daily-v4'
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
$Src = 'C:\path\to\cityheaven-daily-v4'
$Root = 'C:\Users\epuro\Documents\Codex\2026-09-18\windows-execution-foundation'
$files = @(
  'src\windows_execution_foundation\cityheaven_daily_metrics_v4.py',
  'src\windows_execution_foundation\cityheaven_update_count_retry_v4.py',
  'run-cityheaven-daily-metrics-v4.py',
  'run-cityheaven-daily-metrics-logged-v4.py',
  'run-cityheaven-update-count-retry-v4.py',
  'run-cityheaven-update-count-retry-logged-v4.py',
  'tests\__init__.py',
  'tests\offline_guard.py',
  'tests\test_cityheaven_daily_metrics_v4.py',
  'tests\test_cityheaven_update_count_retry_v4.py',
  'tests\test_cityheaven_logged_launcher_v4.py',
  'RUN-v4.md'
)
foreach ($rel in $files) {
  $dest = Join-Path $Root $rel
  New-Item -ItemType Directory -Force -Path (Split-Path -Parent $dest) | Out-Null
  Copy-Item -LiteralPath (Join-Path $Src $rel) -Destination $dest -Force
}
```

v2 / v3 の取得・専用 Chrome（CDP 9227）・`pii_guard` はそのまま使います。取得の runtime は今までどおり `%LOCALAPPDATA%\WindowsExecutionFoundation\cityheaven-daily`（`--runtime`）です。v4 の receipt と logged ランチャーのログは `%LOCALAPPDATA%\WindowsExecutionFoundation\cityheaven-daily-v4`（`--v4-runtime` で変更可）です。

オフラインのテスト（ブラウザ・Node・Google・Airtable・DPAPI にはつなぎません）:

```powershell
& $Python -m unittest tests.test_cityheaven_daily_metrics_v4
& $Python -m unittest tests.test_cityheaven_update_count_retry_v4
& $Python -m unittest tests.test_cityheaven_logged_launcher_v4
```

## 2. dry-run（シートへは書かない）

前日を対象にするときは `--date` を省けます。書く予定の行は標準出力の JSON と、`cityheaven-daily-v4\dry-run\YYYYMMDD.json` に出ます。タブの新規作成もしません。シートが読めないときは、読まずに予定行だけ出します。見出し不一致（`SHEET_SCHEMA_DRIFT`）と値の食い違い（`SHEET_CONTENT_CONFLICT`）は dry-run でも失敗です。終了コードは成功 0、失敗 20、日付が不正なら 2 です。

保存済みの v3 取得がある日は、CityHeaven に取りに行かず Chrome を開かないことがあります。ブラウザを開いた場合も、成功・失敗・例外のあと必ず閉じ、結果の `browser_closed` は true です。店長の普段使い Chrome は `skipped_chrome_pids` のまま触りません。

```powershell
& $Python .\run-cityheaven-daily-metrics-v4.py --dry-run --date 2026-10-21
```

Chrome を開かず、保存済み analysis から行を見るとき:

```powershell
& $Python .\run-cityheaven-daily-metrics-v4.py --dry-run --offline-analysis C:\path\to\analysis.json --date 2026-10-21
```

`--offline-receipt` を足すと、receipt の `reader_sha256` と `raw_source_sha256` を run_log に載せます。

## 3. test-tab（本番タブには書かない）

`--test-tab test` の書き先は次の3タブです。アンダースコアは2つです。

- `city_metric_detail__test`
- `city_daily_summary__test`
- `run_log__test`

無ければ見出し付きで作ります。本番タブは作成も変更もしません。`city_metric_detail` のように本番タブ名を suffix に渡すと `TEST_TAB_REJECTED` で止まります。

```powershell
& $Python .\run-cityheaven-daily-metrics-v4.py --test-tab test --offline-analysis C:\path\to\analysis.json --date 2026-10-21
```

ライブ取得で試しタブへ書くとき（専用 Chrome を使い、終わったら閉じます）:

```powershell
& $Python .\run-cityheaven-daily-metrics-v4.py --test-tab test --date 2026-10-21
```

同じ日をもう一度流すと、dedup_key / summary_key / run_id が一致し値が同じなら追記は 0（replay）です。値が違えば `SHEET_CONTENT_CONFLICT` で何も書きません。revision の自動採番はしません。UNKNOWN は 0 にせず、value は空欄、value_state は `UNKNOWN` です。確定の 0 は 0 のままです。表示名は行に入れません。`store_alias` は空です。PII 走査を通らなければ書きません。

ライブ取得は、receipt が PASS かつ analysis が COMPLETE のときだけシートへ書きます。朝のダッシュ欠損で UNKNOWN 行を残すと、14:30 の確定値と衝突するためです。`--offline-analysis` は後追い用なので PARTIAL を書けます。

## 4. 14:30 の再取得

朝が失敗した日だけ再取得します。判定は v2 と同じです。`NOT_ELIGIBLE` と `ALREADY_RETRIED` はシートへ書かず、終了コード 0 です。

```powershell
& $Python .\run-cityheaven-update-count-retry-v4.py --check-only --date 2026-10-21
& $Python .\run-cityheaven-update-count-retry-v4.py --dry-run --date 2026-10-21
& $Python .\run-cityheaven-update-count-retry-v4.py --test-tab test --date 2026-10-21
```

## 5. 本番（店長の確認のあと）

本番タブへ書く前に、同じタブへ書いている box_parallel 側を止めてください。1タブにつき書き手は1つです。dedup は同じ値の二重追記は止めますが、書き手が2つある状態にはしません。

日次タスク `\TantanManagementOS-CityHeavenDaily-0810` の引数を `run-cityheaven-daily-metrics-logged-v4.py` に向けます。14:30 の `\TantanManagementOS-CityHeavenUpdateCountRetry-1430` の引数を `run-cityheaven-update-count-retry-logged-v4.py` に向けます。タスク名と時刻はそのままです。同名の第二タスクは作りません。このリポジトリの XML は変更しません。

手動で本番タブへ書く例:

```powershell
& $Python .\run-cityheaven-daily-metrics-logged-v4.py --date 2026-10-21
& $Python .\run-cityheaven-update-count-retry-logged-v4.py --date 2026-10-21
```

ログは `%LOCALAPPDATA%\WindowsExecutionFoundation\cityheaven-daily-v4\logs\YYYYMMDD_HHMMSS.log` です。writer は `cityheaven_daily_v4`、mode はライブが `NATURAL`、オフラインが `MANUAL` です。`sheet_write_count` は追記した行数（run_log を含む）です。

## ロールバック

タスク定義の中身は戻さず、引数のスクリプトだけを今の v3 に戻します。

- 日次: `run-cityheaven-daily-metrics-logged-v3.py`
- 14:30: `run-cityheaven-update-count-retry-logged.py`

v4 が本番タブへ書いた行は自動では消しません。試しタブ（`__test` など）は本番タブと別です。receipt と logs は消しません。
