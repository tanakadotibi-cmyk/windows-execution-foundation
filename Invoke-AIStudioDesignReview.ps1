param(
    [Parameter(Mandatory=$true)][string]$PacketPath,
    [Parameter(Mandatory=$true)][string]$OutputPath,
    [Parameter(Mandatory=$true)][string]$JobId
)

$ErrorActionPreference = 'Stop'
if ($JobId -notmatch '^[A-Za-z0-9][A-Za-z0-9._-]{0,119}$') { throw 'INVALID_JOB_ID' }

$repo = [IO.Path]::GetFullPath($PSScriptRoot)
$packet = [IO.Path]::GetFullPath($PacketPath)
$output = [IO.Path]::GetFullPath($OutputPath)
$repoPrefix = $repo.TrimEnd([IO.Path]::DirectorySeparatorChar) + [IO.Path]::DirectorySeparatorChar
if (-not (Test-Path -LiteralPath $packet -PathType Leaf)) { throw 'PACKET_NOT_FOUND' }
if (-not $output.StartsWith($repoPrefix, [StringComparison]::OrdinalIgnoreCase)) { throw 'OUTPUT_OUTSIDE_REPO' }

$python = 'C:\Users\epuro\AppData\Local\TantanManagementOS\CodexBridge\runtime\ai_healer_venv\Scripts\python.exe'
$submit = 'C:\Users\epuro\work\codex-job-completion-bridge-20260921\Submit-ScriptManagedJob.ps1'
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) { throw 'CANONICAL_PYTHON_NOT_FOUND' }
if (-not (Test-Path -LiteralPath $submit -PathType Leaf)) { throw 'COMPLETION_BRIDGE_NOT_FOUND' }

$verifier = $null
$command = Get-Command codex.exe -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
if ($command -and (Test-Path -LiteralPath $command.Source -PathType Leaf)) {
    $verifier = [IO.Path]::GetFullPath($command.Source)
}
if (-not $verifier) {
    $releaseRoot = Join-Path $env:USERPROFILE '.codex\packages\standalone\releases'
    if (Test-Path -LiteralPath $releaseRoot -PathType Container) {
        $candidates = foreach ($directory in Get-ChildItem -LiteralPath $releaseRoot -Directory) {
            $candidate = Join-Path $directory.FullName 'bin\codex.exe'
            if (Test-Path -LiteralPath $candidate -PathType Leaf) {
                try { $version = [version]$directory.Name } catch { continue }
                [pscustomobject]@{ Version = $version; Path = [IO.Path]::GetFullPath($candidate) }
            }
        }
        $verifier = $candidates | Sort-Object Version -Descending | Select-Object -First 1 -ExpandProperty Path
    }
}
if (-not $verifier) { throw 'VERIFIER_NOT_FOUND_FAIL_CLOSED' }

$jobWork = Join-Path $repo (Join-Path 'local\ai-studio-designer-jobs' $JobId)
[void](New-Item -ItemType Directory -Path $jobWork -Force)
$argsFile = Join-Path $jobWork 'args.json'
$specFile = Join-Path $jobWork 'verification-spec.json'
$relativeOutput = $output.Substring($repoPrefix.Length).Replace('\\', '/')

$producerArgs = @(
    '-m', 'windows_execution_foundation.gemini_design_review',
    '--packet-file', $packet,
    '--output-file', $output,
    '--mode', 'explicit'
)
$verificationSpec = [ordered]@{
    version = 1
    criteria = 'Verify the explicit-only AI Studio design review output. The review must be a real successful Gemini review, advisory-only, and report zero external mutations and zero Airtable writes.'
    required_files = @(
        [ordered]@{
            path = $relativeOutput
            kind = 'json'
            json_equals = [ordered]@{
                status = 'PASS'
                review_executed = $true
                trigger_match = $true
                external_mutation_count = 0
                airtable_write_count = 0
            }
        }
    )
    required_output_contains = @('"status":"PASS"', '"review_executed":true')
}
$producerArgs | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $argsFile -Encoding utf8
$verificationSpec | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $specFile -Encoding utf8

$priorPythonPath = [Environment]::GetEnvironmentVariable('PYTHONPATH', 'Process')
try {
    $src = Join-Path $repo 'src'
    $env:PYTHONPATH = if ($priorPythonPath) { $src + [IO.Path]::PathSeparator + $priorPythonPath } else { $src }
    $bridgeOutput = & $submit `
        -JobId $JobId `
        -Workdir $repo `
        -Executable $python `
        -ArgsFile $argsFile `
        -VerificationSpec $specFile `
        -VerifierPath $verifier `
        -VerifierModel 'gpt-5.6-luna' `
        -Reasoning 'medium' `
        -ProducerTimeout 30 `
        -VerifierTimeout 180
    $bridgeExit = $LASTEXITCODE
} finally {
    [Environment]::SetEnvironmentVariable('PYTHONPATH', $priorPythonPath, 'Process')
}

$bridgeResult = $bridgeOutput | Select-Object -Last 1 | ConvertFrom-Json
[ordered]@{
    completion_bridge_job_result = $bridgeResult
    output_path = $output
} | ConvertTo-Json -Depth 8 -Compress
exit $bridgeExit
