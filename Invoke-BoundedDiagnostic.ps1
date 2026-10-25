param(
  [Parameter(Mandatory=$true)][string]$ScriptPath,
  [int]$TimeoutSeconds = 15
)
Set-StrictMode -Version Latest
$ErrorActionPreference='Stop'
if($TimeoutSeconds -lt 1 -or $TimeoutSeconds -gt 60){ throw 'TIMEOUT_OUT_OF_RANGE' }
if(-not(Test-Path -LiteralPath $ScriptPath -PathType Leaf)){ throw 'SCRIPT_NOT_FOUND' }

$psi=New-Object System.Diagnostics.ProcessStartInfo
$psi.FileName=(Join-Path $PSHOME 'powershell.exe')
$psi.Arguments='-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "'+$ScriptPath+'"'
$psi.UseShellExecute=$false
$psi.CreateNoWindow=$true
$psi.WindowStyle=[System.Diagnostics.ProcessWindowStyle]::Hidden
$psi.RedirectStandardOutput=$true
$psi.RedirectStandardError=$true
$p=[System.Diagnostics.Process]::Start($psi)
if(-not $p.WaitForExit($TimeoutSeconds*1000)){
  try{& taskkill.exe /PID $p.Id /T /F | Out-Null}catch{try{$p.Kill()}catch{}}
  Write-Output ('DIAGNOSTIC_TIMEOUT pid='+$p.Id+' timeout='+$TimeoutSeconds)
  exit 124
}
$stdout=$p.StandardOutput.ReadToEnd()
$stderr=$p.StandardError.ReadToEnd()
if($stdout){Write-Output $stdout.TrimEnd()}
if($stderr){[Console]::Error.WriteLine($stderr.TrimEnd())}
exit $p.ExitCode
