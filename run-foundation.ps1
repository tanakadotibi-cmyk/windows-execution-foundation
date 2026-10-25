param(
  [Parameter(Mandatory=$true)][string]$ConfigPath,
  [ValidateSet('work-once','watchdog','audit')][string]$Mode = 'work-once'
)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Config = Get-Content -LiteralPath $ConfigPath -Raw | ConvertFrom-Json
$Python = $Config.python_executable
if (-not $Python) { $Python = (Get-Command python.exe -ErrorAction Stop).Source }
if (-not (Test-Path -LiteralPath $Python)) { throw "Configured Python does not exist: $Python" }
$env:PYTHONPATH = Join-Path $Root 'src'
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUTF8 = '1'
$LogDir = Join-Path $Root 'local\logs'
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$LogPath = Join-Path $LogDir ("foundation-{0}.log" -f (Get-Date -Format 'yyyyMMdd'))
$Arguments = @('-m','windows_execution_foundation','--config',$ConfigPath,$Mode)
if ($Mode -eq 'watchdog') { $Arguments += @() }
& $Python @Arguments 2>&1 | Tee-Object -FilePath $LogPath -Append
exit $LASTEXITCODE
