param(
  [Parameter(Mandatory=$true)][string]$AuditReceiptPath
)

$ErrorActionPreference = 'Stop'
$GhInstaller = 'C:\Users\handl\Documents\Codex\2026-08-20\files-pasted-by-the-user-os\outputs\girls_heaven_optimizer_v02\install_publish_scheduler.ps1'
$Audit = Join-Path (Split-Path -Parent $MyInvocation.MyCommand.Path) 'audit-windows.ps1'
try {
  & $GhInstaller
  & $Audit -ReceiptPath $AuditReceiptPath
} catch {
  $Failure = [ordered]@{
    status = 'FAILED'
    observed_at = [DateTimeOffset]::Now.ToString('o')
    error = $_.Exception.Message
    category = [string]$_.CategoryInfo
    script_stack = $_.ScriptStackTrace
  }
  New-Item -ItemType Directory -Path (Split-Path -Parent $AuditReceiptPath) -Force | Out-Null
  $Failure | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $AuditReceiptPath -Encoding UTF8
  throw
}
