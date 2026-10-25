Option Explicit
Dim shell, fso, root, ps, runner, configPath, cmd, rc
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
root = fso.GetParentFolderName(WScript.ScriptFullName)
ps = shell.ExpandEnvironmentStrings("%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe")
runner = root & "\run-foundation.ps1"
configPath = root & "\local\config.json"
cmd = Chr(34) & ps & Chr(34) & " -NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File " & Chr(34) & runner & Chr(34) & " -ConfigPath " & Chr(34) & configPath & Chr(34) & " -Mode work-once"
rc = shell.Run(cmd, 0, True)
WScript.Quit rc
