param(
  [Parameter(Mandatory=$true)][string]$ConfigPath,
  [Parameter(Mandatory=$true)][string]$ReceiptPath
)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Installer = Join-Path $Root 'install-windows.ps1'
$Arguments = @(
  '-NoProfile',
  '-ExecutionPolicy','Bypass',
  '-File',('"{0}"' -f $Installer),
  '-ConfigPath',('"{0}"' -f $ConfigPath),
  '-ReceiptPath',('"{0}"' -f $ReceiptPath)
)
$Process = Start-Process -FilePath 'powershell.exe' -Verb RunAs -WindowStyle Hidden -ArgumentList $Arguments -Wait -PassThru
exit $Process.ExitCode
