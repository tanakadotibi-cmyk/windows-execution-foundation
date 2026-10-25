# 定時の食い違い：記録と照合手順

この文書は記録と、あとで人が読み取りだけで突き合わせる手順です。実機のタスクは変えません。`install-windows.ps1` の定時も変えていません。配置もしません。

## 記録

| 項目 | 内容 |
| --- | --- |
| リポジトリ `install-windows.ps1` 55行目（ワーカーの繰り返し） | 1分。`New-TimeSpan -Minutes 1` |
| 同ファイル 56行目（ウォッチドッグの繰り返し） | リポジトリ上は同じく1分 |
| 実機報告 | 5分ごと。出典は ai-team-hub #107（DOT監査 #48） |
| この試作で実機を照会したか | いいえ |
| この試作で実機の設定を変えたか | いいえ |
| インストールスクリプトを直したか | いいえ |

機械が読める同じ記録は `docs/schedule-discrepancy-record.json` です。

55行目は次のとおりです。

```powershell
$WorkerTriggers = @((New-ScheduledTaskTrigger -AtLogOn -User $CurrentUser), (New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 1)))
```

1分と5分のどちらを正とするかは、ここでは決めません。DOTの結果確認と店長のOKの前に、定時の定義も実機の登録内容も動かしません。

## 照合手順（読み取りだけ）

実機で次を実行するのは、DOTの確認と店長のOKが出たあとです。この試作の作業では実行しません。

1. リポジトリの `install-windows.ps1` 55行目と56行目を開き、繰り返しが `New-TimeSpan -Minutes 1` のままか確認する。
2. 実機では登録内容の表示だけを行う。変更用のコマンドは使わない。

```powershell
Get-ScheduledTask -TaskName 'ExecutionFoundation-Worker','ExecutionFoundation-Watchdog' |
  ForEach-Object {
    [pscustomobject]@{
      TaskName = $_.TaskName
      State = $_.State
      Repetition = ($_.Triggers | ForEach-Object { $_.Repetition.Interval }) -join ','
    }
  }
```

3. 表示された `Repetition` を、リポジトリの1分（`PT1M`）と、報告にある5分（`PT5M`）のどちらに近いかメモする。タスク名、繰り返し、確認した時刻だけを残す。
4. 1分のままにするか、5分に合わせるかは、そのメモをDOTと店長が見てから決める。決まるまで `install-windows.ps1` を実機で実行しない。

## 使わないもの

- `install-windows.ps1` の実行
- `Register-ScheduledTask`
- `schtasks /Change`、`schtasks /Create`、`schtasks /Delete`、`schtasks /Run`
- タスク定義の上書き、間隔の書き換え、電源設定の変更
