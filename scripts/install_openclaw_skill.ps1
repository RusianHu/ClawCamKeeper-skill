$ErrorActionPreference = 'Stop'

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$repoName = Split-Path $projectRoot -Leaf
$targetRoot = Join-Path $env:USERPROFILE '.openclaw\workspace\skills'
$targetDir = Join-Path $targetRoot 'clawcamkeeper-openclaw'
$launcherPath = Join-Path $targetDir 'scripts\invoke-clawcamkeeper-openclaw.ps1'

$excludeDirs = @(
    '.git',
    '.ace-tool',
    '__pycache__',
    '.pytest_cache',
    '.mypy_cache',
    '.ruff_cache',
    '.venv',
    'venv',
    'node_modules'
)

$excludeFiles = @(
    '*.log',
    '*.pyc',
    '*.pyo'
)

if (-not (Test-Path (Join-Path $projectRoot 'main.py'))) {
    throw "未找到项目入口脚本: $(Join-Path $projectRoot 'main.py')"
}

if (-not (Test-Path (Join-Path $projectRoot 'SKILL.md'))) {
    throw "未找到仓库根技能文件: $(Join-Path $projectRoot 'SKILL.md')"
}

New-Item -ItemType Directory -Force -Path $targetRoot | Out-Null
New-Item -ItemType Directory -Force -Path $targetDir | Out-Null

$robocopyArgs = @(
    $projectRoot,
    $targetDir,
    '/MIR',
    '/R:1',
    '/W:1',
    '/NFL',
    '/NDL',
    '/NJH',
    '/NJS',
    '/NP',
    '/XD'
) + $excludeDirs + @('/XF') + $excludeFiles

& robocopy @robocopyArgs | Out-Null
$robocopyExit = $LASTEXITCODE
if ($robocopyExit -ge 8) {
    throw "robocopy 同步临时镜像失败，退出码: $robocopyExit"
}

$metadata = [ordered]@{
    skill_name = 'clawcamkeeper-openclaw'
    install_mode = 'self_contained_repository'
    source_repo_name = $repoName
    source_repo_path = $projectRoot
    installed_at = (Get-Date).ToString('o')
    installed_to = $targetDir
    notes = @(
        '此技能目录包含完整项目代码副本',
        '应直接在当前 skill 工作区根目录运行 main.py 与 requirements.txt',
        '更新时可重新执行 scripts/install_openclaw_skill.ps1 覆盖同步'
    )
}

$metadataPath = Join-Path $targetDir 'skill-install.json'
$metadata | ConvertTo-Json -Depth 6 | Set-Content -Path $metadataPath -Encoding utf8

$launcherContent = @'
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
'@

$launcherDir = Split-Path -Parent $launcherPath
New-Item -ItemType Directory -Force -Path $launcherDir | Out-Null
Set-Content -Path $launcherPath -Value $launcherContent -Encoding utf8

Write-Host "已同步完整技能仓库到: $targetDir"
Write-Host "仓库根技能文件: $(Join-Path $targetDir 'SKILL.md')"
Write-Host "安装元信息: $(Join-Path $targetDir 'skill-install.json')"
Write-Host "包装脚本: $launcherPath"
Write-Host "入口脚本: $(Join-Path $targetDir 'main.py')"
Write-Host "可用文件数量: $((Get-ChildItem $targetDir -Recurse -File | Measure-Object).Count)"
Write-Host "目录预览:"
Get-ChildItem $targetDir | Select-Object Name, FullName | Format-Table -AutoSize
