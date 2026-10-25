# Windows Execution Foundation v1

> 試作 1.1.0（ai-team-hub #107）。実機への配置・タスクの切り替えは含まない。定時1分と実機報告5分の食い違いは [docs/windows-schedule-reconciliation.md](docs/windows-schedule-reconciliation.md) の記録と照合手順だけ。

Remote-control-independent execution foundation for Windows automation jobs.

## Guarantees

- durable local spool (`pending`, `inflight`, `done`, `failed`, `unknown`)
- one execution id per job, with an external receipt file for that id only
- a success receipt must match the execution id, the action, a success status, and a file written after that attempt started; anything else is `RESULT_UNKNOWN` and is not resent
- before another attempt, the external result is reconciled; an effect that cannot be confirmed is not treated as unrun
- one worker at a time through a file lock, plus an ownership lease that is refreshed while that process lives
- heartbeat age (default 120s) is a watchdog alert; the lease (default 300s) is the reclaim deadline for a dead owner; the child timeout (default 900s) is enforced by the owner. A live owner is not reclaimed
- audit judges queue age against the allowed delay separately, and matches each done job to its receipt by execution id
- action allowlist; spool files cannot inject arbitrary commands
- network-aware retry without dropping queued work
- Task Scheduler installation script remains the checked-in definition; this prototype does not apply it

## Exit codes

| Status | Code |
| --- | --- |
| `SUCCESS`, `ENQUEUED`, `NOOP_RECEIPT`, `NOOP_QUEUED`, `IDLE`, `HEALTHY`, `PASS`, `NOOP_SINGLE_WORKER` | 0 |
| `RETRY_SPOOLED` | 10 |
| `RESULT_UNKNOWN` | 11 |
| `FAILED_FINAL` | 12 |
| `ATTENTION` | 13 |
| `WORKER_STALE` | 14 |
| `WORKER_UNRESPONSIVE` | 15 |
| `DROPPED_BEFORE_LOCAL_RECEIPT` | 16 |
| `NOOP_UNKNOWN` | 17 |
| any other status | 1 |

Machine-specific paths and credentials belong in `local/config.json` and are ignored by Git.
Start from `config.example.json`.

## Commands

```powershell
python -m windows_execution_foundation --config .\local\config.json enqueue --action gh-verify --idempotency-key example
python -m windows_execution_foundation --config .\local\config.json work-once
python -m windows_execution_foundation --config .\local\config.json watchdog
python -m windows_execution_foundation --config .\local\config.json audit
```

The checked-in installer is unchanged. Do not run it for this prototype; compare the schedule with the read-only procedure in `docs/windows-schedule-reconciliation.md` after DOT review and owner approval.

```powershell
.\install-windows.ps1 -ConfigPath .\local\config.json
```

## Tests

```powershell
python -m unittest discover -s tests -v
```

The acceptance suite includes three accelerated runs covering Remote unavailable,
worker/browser crash recovery, network outage recovery, duplicate enqueue, and noop replay.
`tests/test_issue107.py` covers receipt identity, unknown effects, queue delay, ownership deadlines, exit codes, and the schedule record.
