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

## CODEX_LOCAL_JOB lanes A and B (2026-09-25)

The production configuration enables two instances of this same core. Existing
requests without `request.lane` belong to A. To target B keep action type
`CODEX_LOCAL_JOB` and envelope schema `codex_local_job_mailbox_v1`; add to `request`:

```json
{"lane": "B", "resource_scope": "repo", "resource_key": "windows-execution-foundation"}
```

The Action field in the existing Next Actions table retains the JSON envelope.
No new Airtable fields, tables, bases, UI, credentials, services or notification
systems are required. B invokes Run-CodexManagedJob.ps1 directly, like A; it never
dispatches a job back to A. Request IDs remain global and immutable across lanes.
The shared Completion Core retains the original `codex-local-<request hash>` job
identity, verifier and exactly-once notification attempt contract.

| Item | A | B |
|---|---|---|
| Worker task | ExecutionFoundation-Worker | ExecutionFoundation-Worker-B |
| Runner | run-worker-hidden.pyw | same runner, --lane B |
| State | existing configured state_dir | state_dir/lanes/B |
| Spool, inflight, receipts, locks, temporary input, result file | existing paths | beneath B state |
| Airtable, credentials, Completion, Verifier, Slack | shared | shared |

`load_config` derives B configuration in memory. There is no second codebase or
copied config to maintain. The existing watchdog checks both lanes. Each worker
cycle drains at most two jobs (including jobs queued during the first); the task
time limit is 40 minutes, covering two 900s actions. Queue tails beyond two jobs,
future retries and resource conflicts still use the existing five-minute trigger.

New producers use envelope schema `codex_local_job_mailbox_v2`. Its `request`
must contain `work_number` in `YYYYMMDD-HHMMSS` or `YYYYMMDD-HHMMSS-NN` form,
and it must exactly match the existing Next Actions 作業番号 field. Legacy v1
envelopes remain valid without that member. The accepted number is retained in
the job metadata, result, and Foundation receipt.

Resource leases live in `state_dir/shared-resource-locks`. One short Windows file
lock protects admission and release; it is never held for the duration of a job.
Matching resource scope/key or overlapping canonical workdirs cannot overlap.
A missing resource declaration is a wildcard conflicting with every resource,
including other Foundation actions. Different declarations are safe only when
they accurately describe the intended work. Use the same stable key for a shared
repo/service/profile; declare unknown or multi-resource work without a key to
obtain whole-foundation exclusion. Job sandbox and existing allowlists still apply.

Leases are durable through worker death, outer timeout or unconfirmed child
termination. They have no unsafe TTL-based auto-reclaim. A blocked job stays in
pending without consuming an attempt. A retained lease requires operator
investigation: inspect its request ID and the existing Completion status; confirm
all producer/verifier descendants have exited before any manual release. Do not
remove a lease merely because its worker PID or its heartbeat is stale. Historical
released leases record measured admission/release timestamps. Existing pre-upgrade
A spool items without resource metadata obtain wildcard protection.

Rollback: stop admitting B jobs, wait for both tasks to finish, ensure both
inflight directories and shared active leases are empty, then use the staged
`rollback.ps1`. It restores original source/config/task XML and unregisters only
the newly added B task. It refuses rollback over unknown later edits. Preserve
all receipts, shared request admissions and acceptance evidence. After rollback
B rows must not be queued until dual-lane code is restored.
