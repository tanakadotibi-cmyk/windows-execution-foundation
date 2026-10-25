# LANE_MONITORING_AND_NOTIFICATION_CONTRACT_v1

作成: 2026-09-26 JST（店長GO 2026-09-26 17:01 JST）／ 対象: M720S Windows Execution Foundation と Completion Bridge（MAIN・M720S）

## 1. 2つのレーン
| レーン | タスク | 役割 |
|---|---|---|
| Lane A | `ExecutionFoundation-Worker`（5分ごと） | 全アクション。11時の minkue-public-fetch（本線）を含む |
| Lane B | `ExecutionFoundation-Worker-B`（5分ごと, `--lane B`） | `codex-local-job` のみ |
| Watchdog | `ExecutionFoundation-Watchdog`（5分ごと） | `run-watchdog-hidden.pyw` が A と B の両方を順に確認（lanes.enabled=true のとき） |

- 状態: `%LOCALAPPDATA%\WindowsExecutionFoundation`（A）、`...\lanes\B`（B）
- Worker の5分周期は変更しない。

## 2. 共有ロック（lease）のルール
- 置き場: `shared-resource-locks\active\<token>.json`（token, lane_id, request_id, resource, worker_pid, acquired_at）
- resource 未指定のジョブは key `*` = すべてと衝突。同じ key、または親子関係の workdir も衝突。
- 解放は持ち主 worker が完了確認後に `released\` へ移す。**経過時間では絶対に解放しない。**
- 【2026-09-26 追加】持ち主死亡時の自動回収（次の acquire 時、registry_lock 内）:
  - worker_pid が存在しない／終了済み／python(w).exe 以外／lease 取得後に起動した別プロセス（PID再利用）／他ユーザーのプロセス → 持ち主死亡と判定
  - ただし次の場合は回収しない（安全側）: 死んだ worker の子プロセスが生きている、failed スプールのジョブがその lease を保持している（fail-closed＝手動救出）、判定中にエラー
  - 回収時: `released\<token>.json` に `released_reason=OWNER_DEAD_RECLAIMED` を付けて移動し、Slack 通知（失敗しても worker は止めない）

## 3. Slack 通知一覧（Foundation）
| 通知 | 条件 | 頻度 |
|---|---|---|
| 共有ロック自動回収 | 上記の回収が起きた | 回収のたび |
| RESOURCE_WAIT 継続 | 同じレーンで RESOURCE_WAIT が15分以上連続（状態: `<state>\monitoring\resource-wait.json`） | 初回＋最大1時間に1回。解消時に1回「解消」通知 |
| watchdog: WORKER_STALE | worker 心拍が15分超で worker ロック無し（再起動を試行） | 条件ごと1時間に最大1回 |
| watchdog: RESTART_FAILED | schtasks での再起動失敗 | 同上 |
| watchdog: WORKER_BUSY_STALE_HEARTBEAT | ロック保持中で心拍が「最長アクションtimeout+300秒（現設定20分）」超 | 同上（状態: `<state>\monitoring\watchdog-alerts.json`） |
| 既存: Foundation 作業結果/終端失敗 | foundation_failure_slack（従来どおり） | 1ジョブ1回 |

- 送信先: User 環境変数 `TANTAN_SLACK_WEBHOOK_URL` / `TANTAN_SLACK_NOTIFY_USER_ID`（値は表示・保存しない）。`config.foundation_failure_slack.enabled=true` のときのみ送信。テスト時は `WEF_DISABLE_MONITOR_SLACK=1` で無効化。

## 4. Completion Bridge Sweeper（MAIN・M720S 両方）
- タスク: `CompletionBridge-Sweeper`（15分ごと, ユーザー権限, pythonw で非表示）
- スクリプト: `<bridge>\monitoring\completion_bridge_sweeper.py`（jobs/admissions は**読むだけ**）
- 検知: ①非終端（VERIFIED/FAILED/VERIFY_FAILED 以外）が60分超 ②終端なのに notification が SENT 以外（NOT_CONFIGURED/UNKNOWN/HTTP_FAILED/空）③admission が ADMITTED のまま60分超 ④status.json 欠損/破損
- 通知: 1回の実行で新規分を1通にまとめて送信。同じ「ジョブ×問題」は1回だけ（状態: `%LOCALAPPDATA%\CompletionBridgeSweeper\state.json`、ログ: 同 `logs\`）
- クローズ扱い: job フォルダに `closed-by-audit.json` がある／admission が `CLOSED` か `ABANDONED`

## 5. 2026-09-26 に塞いだ穴
1. worker クラッシュ後に lease が残り、両レーンが無言で永久停止する → 持ち主死亡 lease の自動回収
2. RESOURCE_WAIT が IDLE と同じ扱いで無言 → 15分継続で Slack
3. watchdog の再起動・異常が無言 → Slack（A/B 両方。B は既に watchdog pyw で巡回済みを確認）
4. Completion Bridge の未完了・未通知・ADMITTED 放置が無言 → Sweeper
5. 放置項目のクローズ: MAIN task72/task78 は正式 reconcile で CLOSED、task64（MAIN）/task70（M720S）はジョブ未作成のため ABANDONED 記録、M720S の 9/21 PoC 6件は `closed-by-audit.json` を追加（status.json は無変更）

## 6. ロールバック
- Foundation: `windows-execution-foundation\backup-20260926-lanefix\ROLLBACK.md`（lanes.py/core.py/cli.py を戻すだけ。lane_monitor.py は未参照になる）
- Sweeper: `Disable-ScheduledTask -TaskName CompletionBridge-Sweeper`（各PC）
- クローズ: 各 bridge の `backups\backup-20260926-lanefix\ROLLBACK.md`

## 7. MAIN 追補（2026-09-26 17:21 GO）
- **退役済み ExecutionFoundation-Worker（MAIN）**: `foundation-retired.json` により起動直後に RETIRED で終了するだけのタスク。XML を `backups\backup-20260926-lanefix-main\` に退避済み。無効化は**管理者権限が必要（アクセス拒否）で未実施**。管理者 PowerShell で `Disable-ScheduledTask -TaskName ExecutionFoundation-Worker`（削除はしない）。依存する仕組みは見つからず。Foundation watchdog タスクは MAIN に存在しない。
- **admit（Assert-CompletionCoverage）が使われていない原因**: admit は「コントローラーが自主的に呼ぶ」手順で、`Submit-*ManagedJob.ps1` は admit を呼ばない。さらに admit には task_no・operation_class が必須だが、9/24・9/26 の MAIN ジョブは TaskNo なしで Submit を直接実行していた。Submit 側の自動 admit は operation_class が分からず（read_only は admission 不要）、admit 後の reconcile を誰も実行しないため ADMITTED 放置が増える。M720S の Foundation Lane B も同じエンジン経路を使っているので、今回は変更しない。
- **UNADMITTED_JOB 検知（Sweeper）**: MAIN の Sweeper で `--unadmitted-since 2026-09-26T18:00:00+09:00` を有効化。この時刻以降に作られたジョブで admission ファイルが無いものを1ジョブ1回だけ通知（read_only 免除の可能性も文面に明記）。M720S では無効（Lane B の codex-local-* が admit しない設計のため）。
- **Codex Bridge watcher の生存監視**: MAIN `TantanOS-CodexBridge-Main-Highest`（bridge_watcher.py）と M720S `TantanOS-CodexBridge-Highest`（bridge_launcher.py）は、どちらも 2026-09-24 に店長判断で **RETIRED**（`runtime\watcher-retired.json`）。現在はプロセス無しが正常。Sweeper（MAIN）は watcher-retired.json を尊重し、RETIRED の間は通知しない。マーカーが外れているのにプロセスが10分以上無い場合: Slack 1回（以後1時間に最大1回）、`schtasks /run` での自動起動を1回だけ試して結果を記載、復旧時に1回通知。watcher は毎ループで心拍やログを書かないため、判定はプロセス有無（watcher.log の経過時間は参考表示のみ）。
- **ロールバック**: `C:\Users\handl\work\codex-completion-bridge-main\backups\backup-20260926-lanefix-main\ROLLBACK.md`


2026-10-18 店長承認：M720SのCompletionBridge-Sweeperを停止（無効化のみ）。Lane B（Worker-B）は停止保留（停止するとWatchdogがWORKER_STALE/RESTART_FAILEDをSlackへ毎時通知するため。設定だけでは除外不可）。CodexBridge-Highestは管理者権限が必要なため停止保留。Watchdogは後日みんクエ専用プログラムに統合予定。メインPC側は未変更。

2026-10-18 店長承認：WatchdogをB除外に改修し、Lane B（Worker-B）も停止（無効化のみ）。（改修 13:45 JST・config lanes.watchdog_lanes=["A"]、B無効化 13:45 JST、確認 14:15 JST までWatchdog全周期HEALTHY・B通知なし、ロールバック C:\TantanJobs\backup\watchdog-20261018\ROLLBACK.md）
