# Compare pythonw exit codes: run_logged_v3 vs run_logged (v2).
# A dummy child prints and exits 0. pythonw has no stdout/stderr.
# Expected: v3 ExitCode=0, v2 ExitCode=1.
$ErrorActionPreference = "Stop"

$repo = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
Set-Location -LiteralPath $repo

function Get-Pythonw {
    $known = Join-Path $env:LOCALAPPDATA "TantanManagementOS\pyenv-tasks\Scripts\pythonw.exe"
    if (Test-Path -LiteralPath $known) {
        return (Resolve-Path -LiteralPath $known).Path
    }
    $cmd = Get-Command pythonw.exe -ErrorAction SilentlyContinue
    if ($cmd -and $cmd.Source) {
        return $cmd.Source
    }
    throw "pythonw.exe was not found."
}

function ConvertTo-PyString([string]$Value) {
    $backslash = [string][char]92
    $quote = [string][char]39
    $escaped = $Value.Replace($backslash, $backslash + $backslash).Replace($quote, $backslash + $quote)
    return $quote + $escaped + $quote
}

function Invoke-LoggedCheck([string]$Pythonw, [string]$Which, [string]$Work) {
    New-Item -ItemType Directory -Force -Path $Work | Out-Null
    $dummy = Join-Path $Work "dummy_child.py"
    @(
        "import sys"
        "print('dummy-stdout')"
        "print('dummy-stderr', file=sys.stderr)"
        "raise SystemExit(0)"
    ) | Set-Content -LiteralPath $dummy -Encoding utf8

    if ($Which -eq "v3") {
        $module = "cityheaven_logged_launch_v3"
        $fn = "run_logged_v3"
    } else {
        $module = "cityheaven_logged_launch_v2"
        $fn = "run_logged"
    }
    $driver = Join-Path $Work "drive_$Which.py"
    $repoLit = ConvertTo-PyString $repo
    $dummyLit = ConvertTo-PyString $dummy
    $workLit = ConvertTo-PyString $Work
    @(
        "import sys"
        "from pathlib import Path"
        "sys.path.insert(0, ${repoLit})"
        "from src.windows_execution_foundation.${module} import ${fn}"
        "code = ${fn}(Path(${dummyLit}), [], runtime=Path(${workLit}), cwd=Path(${repoLit}))"
        "raise SystemExit(code)"
    ) | Set-Content -LiteralPath $driver -Encoding utf8

    $argument = $driver
    if ($driver -match "\s") {
        $argument = '"' + $driver + '"'
    }
    $proc = Start-Process -FilePath $Pythonw -ArgumentList $argument -WorkingDirectory $repo -WindowStyle Hidden -Wait -PassThru
    Write-Output ("{0} ExitCode={1}" -f $Which, $proc.ExitCode)
    $logDir = Join-Path $Work "logs"
    if (Test-Path -LiteralPath $logDir) {
        $log = Get-ChildItem -LiteralPath $logDir -Filter *.log | Select-Object -First 1
        if ($log) {
            foreach ($line in @(Get-Content -LiteralPath $log.FullName)) {
                if ($line -like "exit_code=*" -or $line -like "launcher_exit_code=*") {
                    Write-Output ("{0} log {1}" -f $Which, $line)
                }
            }
        }
    }
}

$pythonw = Get-Pythonw
$stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$base = Join-Path $env:TEMP "cityheaven_exit_check_$stamp"
Write-Output ("pythonw=" + $pythonw)
Invoke-LoggedCheck $pythonw "v3" (Join-Path $base "v3")
Invoke-LoggedCheck $pythonw "v2" (Join-Path $base "v2")
