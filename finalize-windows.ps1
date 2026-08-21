param([Parameter(Mandatory=$true)][string]$AuditReceiptPath)

$ErrorActionPreference = 'Stop'
w32tm.exe /resync /rediscover
Start-ScheduledTask -TaskName 'GHBlog-SessionHost'
Start-Sleep -Seconds 3
& (Join-Path (Split-Path -Parent $MyInvocation.MyCommand.Path) 'audit-windows.ps1') -ReceiptPath $AuditReceiptPath
