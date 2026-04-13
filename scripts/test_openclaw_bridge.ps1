$ErrorActionPreference = 'Stop'

$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $projectRoot

$commands = @(
    'python .\main.py openclaw status',
    'python .\main.py openclaw doctor',
    'python .\main.py openclaw events --limit 5',
    'python .\main.py openclaw notifications --since-id 0 --limit 5',
    'python .\main.py openclaw config-show',
    'python .\main.py openclaw arm',
    'python .\main.py openclaw status',
    'python .\main.py openclaw action-test',
    'python .\main.py openclaw notifications --since-id 0 --limit 5',
    'python .\main.py openclaw disarm',
    'python .\main.py openclaw status'
)

foreach ($command in $commands) {
    Write-Host "=== $command ===" -ForegroundColor Cyan
    Invoke-Expression $command
    Write-Host ""
}
