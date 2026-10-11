# Run from this repository with its own virtual environment, or Python on PATH.
$agentProject = $PSScriptRoot
$agentPython = Join-Path $agentProject '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $agentPython)) {
    $agentPythonCommand = Get-Command python -ErrorAction SilentlyContinue
    if (-not $agentPythonCommand) {
        throw 'Python 3.11+ is required. Follow the README to create .venv and install requirements.txt.'
    }
    $agentPython = $agentPythonCommand.Source
}
$agentOldUtf8 = $env:PYTHONUTF8
$env:PYTHONUTF8 = '1'
Push-Location -LiteralPath $agentProject
try {
    & $agentPython -m support_agent @args
    $agentExitCode = $LASTEXITCODE
} finally {
    Pop-Location
    $env:PYTHONUTF8 = $agentOldUtf8
}
exit $agentExitCode
