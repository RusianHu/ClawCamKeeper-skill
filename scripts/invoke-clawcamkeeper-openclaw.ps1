param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Arguments = @()
)

$ErrorActionPreference = 'Stop'
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$skillRoot = Split-Path -Parent $scriptDir
$mainScript = Join-Path $skillRoot 'main.py'

if (-not (Test-Path $mainScript)) {
    throw "未找到技能仓库入口脚本: $mainScript"
}

Set-Location $skillRoot
& python $mainScript openclaw @Arguments
exit $LASTEXITCODE
