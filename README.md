# Windows Execution Foundation v1

Remote-control-independent execution foundation for Windows automation jobs.

## Guarantees

- durable local spool (`pending`, `inflight`, `done`, `failed`)
- stable idempotency keys and immutable receipts
- one worker at a time through a Windows file lock
- worker heartbeat plus an independent watchdog heartbeat
- stale inflight recovery and bounded exponential retry
- action allowlist; spool files cannot inject arbitrary commands
- network-aware retry without dropping queued work
- Task Scheduler installation with history, retry, startup/logon triggers

Machine-specific paths and credentials belong in `local/config.json` and are ignored by Git.
Start from `config.example.json`.

## Commands

```powershell
python -m windows_execution_foundation --config .\local\config.json enqueue --action gh-verify --idempotency-key example
python -m windows_execution_foundation --config .\local\config.json work-once
python -m windows_execution_foundation --config .\local\config.json watchdog
python -m windows_execution_foundation --config .\local\config.json audit
```

Install Task Scheduler and Windows reliability settings from an elevated PowerShell:

```powershell
.\install-windows.ps1 -ConfigPath .\local\config.json
```

## Tests

```powershell
python -m unittest discover -s tests -v
```

The acceptance suite includes three accelerated runs covering Remote unavailable,
worker/browser crash recovery, network outage recovery, duplicate enqueue, and noop replay.
