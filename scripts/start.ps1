<# Windows 的兼容启动入口；与 Makefile 使用同一 Compose 定义，不在宿主机运行应用。 #>
[CmdletBinding()]
param(
    [ValidateSet('production', 'development')]
    [string]$Mode = 'production',
    [switch]$EnableModelCalls,
    [ValidatePattern('^knowledgeops(?:-agent|-clean-[a-f0-9]{12})$')]
    [string]$ProjectName = 'knowledgeops-agent'
)
$ErrorActionPreference = 'Stop'
$projectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
if ($Mode -eq 'development' -and $PSBoundParameters.ContainsKey('EnableModelCalls')) {
    throw 'Development reads MODEL_CALLS_ENABLED from .env when dev.py starts; do not use -EnableModelCalls.'
}
if (-not (Test-Path -LiteralPath (Join-Path $projectRoot '.env'))) {
    throw 'Copy .env.example to .env and set POSTGRES_PASSWORD before startup.'
}
function Invoke-Compose {
    param([string[]]$Arguments)
    & docker compose -p $ProjectName @Arguments
    if ($LASTEXITCODE -ne 0) { throw 'Docker Compose failed; inspect the output above.' }
}
$previousModelSetting = $env:MODEL_CALLS_ENABLED
Push-Location $projectRoot
try {
    if ($PSBoundParameters.ContainsKey('EnableModelCalls')) {
        $env:MODEL_CALLS_ENABLED = $EnableModelCalls.ToString().ToLowerInvariant()
    }
    $composeFiles = @('-f', 'compose.yaml')
    if ($Mode -eq 'development') { $composeFiles += @('-f', 'compose.dev.yaml', '--profile', 'development') }
    # 停写后初始化；保留数据库、工作目录与所有持久卷。
    $stopServices = @('api', 'worker', 'web', 'dev')
    Invoke-Compose -Arguments (@('stop') + $stopServices)
    Invoke-Compose -Arguments @('rm', '-f', 'init')
    Invoke-Compose -Arguments ($composeFiles + @('up', '-d', '--build', '--wait'))
    if ($Mode -eq 'production') {
        Invoke-Compose -Arguments @('exec', '-T', 'worker', 'python', 'scripts/wait_for_worker.py')
    }
    Invoke-Compose -Arguments @('--profile', 'development', 'ps')
    if ($Mode -eq 'development') {
        Write-Output 'Development environment ready. Inside the dev container run: python scripts/dev.py'
    } else {
        Write-Output 'Application ready. Default http://localhost:8787; see KNOWLEDGEOPS_PORT in .env.'
    }
} finally {
    $env:MODEL_CALLS_ENABLED = $previousModelSetting
    Pop-Location
}
