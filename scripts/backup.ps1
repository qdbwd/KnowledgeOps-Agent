<#
本机 Docker 维护入口。备份业务库、独立费用账本与不可变原件。
restore 仅允许新建的 knowledgeops_restore_* 空数据库和全新来源卷；不覆盖
正常业务库、不回滚当前费用账本、不删除卷。业务恢复后仍需启动恢复校验。
#>
[CmdletBinding()]
param(
    [ValidateSet('create', 'restore')][string]$Mode = 'create',
    [string]$BackupFolder,
    [ValidateSet('deployment', 'development')][string]$Runtime = 'deployment',
    [string]$WorkspacePath,
    [switch]$IncludeMcpData,
    [ValidatePattern('^knowledgeops(?:_backupfixture_[a-f0-9]{12})?$')][string]$Database = 'knowledgeops',
    [ValidatePattern('^knowledgeops-agent-backupfixture-[a-f0-9]{12}$')][string]$SourceVolume,
    [ValidatePattern('^knowledgeops_restore_[a-f0-9]{12}$')][string]$RestoreDatabase,
    [ValidatePattern('^knowledgeops-agent-restore-[a-f0-9]{12}$')][string]$RestoreVolume
)
$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $true
$projectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$backupBase = Join-Path $projectRoot 'artifacts/backups'
$backupId = [guid]::NewGuid().ToString('N').Substring(0, 12)
if (-not $BackupFolder) { $BackupFolder = Join-Path $backupBase $backupId }
$BackupFolder = [IO.Path]::GetFullPath($BackupFolder)
if (-not $BackupFolder.StartsWith($backupBase + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'Backup folder must be inside this project artifacts/backups.'
}
Push-Location $projectRoot
try {
    $pg = (docker compose ps -q postgres).Trim()
    if (-not $pg) { throw 'Start this project postgres container first.' }
    $backupInDev = '/workspace/' + [IO.Path]::GetRelativePath($projectRoot, $BackupFolder).Replace('\', '/')
    $dev = $null
    if ($Runtime -eq 'development') {
        $dev = (docker compose -f compose.yaml -f compose.dev.yaml ps -q dev).Trim()
        if (-not $dev) { throw 'Start this project dev container first.' }
        if ($SourceVolume -or $RestoreVolume) { throw 'Development backups use an isolated workspace directory, not a named volume.' }
        if ($WorkspacePath -and ($Database -eq 'knowledgeops' -or $WorkspacePath -notmatch '^/workspace/artifacts/verification/backup-[a-f0-9]{12}/workspace$')) {
            throw 'An explicit development workspace is allowed only for a dedicated backup fixture.'
        }
    } elseif ($WorkspacePath) { throw 'WorkspacePath is supported only for development fixtures.' }
    if ($Mode -eq 'create') {
        if ($Runtime -eq 'development' -and $Database -ne 'knowledgeops' -and -not $WorkspacePath) {
            throw 'A fixture backup requires its explicit isolated workspace.'
        }
        if (Test-Path -LiteralPath $BackupFolder) { throw 'Use a new backup folder; existing backups are immutable.' }
        New-Item -ItemType Directory -Path $BackupFolder | Out-Null
        docker compose exec -T postgres pg_dump -U knowledgeops -d $Database -Fc --file="/tmp/$backupId-business.dump"
        # 测试种子必须使用自己的回执库，不复制正式任务预算来伪装隔离验证。
        $accountingSource = "${Database}_accounting"
        docker compose exec -T postgres pg_dump -U knowledgeops -d $accountingSource -Fc --file="/tmp/$backupId-accounting.dump"
        docker cp "${pg}:/tmp/$backupId-business.dump" (Join-Path $BackupFolder 'business.dump')
        docker cp "${pg}:/tmp/$backupId-accounting.dump" (Join-Path $BackupFolder 'accounting.dump')
        if ($Runtime -eq 'development') {
            $sourceWorkspace = if ($WorkspacePath) { $WorkspacePath } else { '/data/agent-workspace' }
            docker exec $dev tar -C $sourceWorkspace -czf "$backupInDev/workspace.tar.gz" .
            $format = 2
            $fileNames = @('business.dump', 'accounting.dump', 'workspace.tar.gz')
        } elseif ($SourceVolume) {
            # 测试种子使用单独卷；只读挂载，不能更改原件。
            docker compose run --rm --no-deps -T -e MODEL_CALLS_ENABLED=false -v "${BackupFolder}:/backup" -v "${SourceVolume}:/backup-sources:ro" api tar -C /backup-sources -czf /backup/sources.tar.gz .
            $format = 1
            $fileNames = @('business.dump', 'accounting.dump', 'sources.tar.gz')
        } else {
            # 文件正文已成为事实来源；只备份 PG 或旧 source-data 卷都不完整。
            docker compose run --rm --no-deps -T -e MODEL_CALLS_ENABLED=false -v "${BackupFolder}:/backup" api tar -C /data/agent-workspace -czf /backup/workspace.tar.gz .
            $format = 2
            $fileNames = @('business.dump', 'accounting.dump', 'workspace.tar.gz')
        }
        # 旧来源卷备份保留格式1；完整工作区增加可移植扩展归档，不打包环境/缓存/密钥。
        if ($format -eq 2) {
            $extensionArgs = @('scripts/backup_extensions.py', '--database', $Database)
            if ($IncludeMcpData) { $extensionArgs += '--include-data' }
            if ($Runtime -eq 'development') {
                $extensionArgs += @('--output', "$backupInDev/extensions.tar.gz")
                if ($WorkspacePath) { $extensionArgs += @('--fixture-root', ($WorkspacePath -replace '/workspace$', '/extensions')) }
                docker exec -w /workspace -e MODEL_CALLS_ENABLED=false $dev .venv/bin/python scripts/dev.py --run .venv/bin/python @extensionArgs
            } else {
                $extensionArgs += @('--output', '/backup/extensions.tar.gz')
                docker compose run --rm --no-deps -T -e MODEL_CALLS_ENABLED=false -v "${BackupFolder}:/backup" api python @extensionArgs
            }
            $format = 3
            $fileNames += 'extensions.tar.gz'
        }
        $hashes = @{}
        foreach ($name in $fileNames) {
            $hashes[$name] = (Get-FileHash -Algorithm SHA256 -LiteralPath (Join-Path $BackupFolder $name)).Hash.ToLowerInvariant()
        }
        @{
            format = $format; created_at = [DateTime]::UtcNow.ToString('o'); source_database = $Database
            source_accounting_database = $accountingSource
            files = $hashes
            extensions_policy = 'Complete skills and MCP locks; no dependency environments, caches or project secrets. Restored MCP connections require explicit preparation, testing and authorization.'
            mcp_data_included = [bool]$IncludeMcpData
            accounting_policy = 'Restore only into a fresh isolated database; never overwrite active task budgets or model replay receipts.'
            consistency = 'Stop api/worker and local file editors before backup. PG pointers and the complete workspace form one backup set; immutable files may include harmless orphans.'
        } | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $BackupFolder 'manifest.json') -Encoding utf8
        Write-Output "Backup: $BackupFolder"
    } else {
        $manifest = Get-Content -Raw -LiteralPath (Join-Path $BackupFolder 'manifest.json') | ConvertFrom-Json
        if ($manifest.format -notin @(1, 2, 3)) { throw 'Unsupported backup format.' }
        $fileNames = if ($manifest.format -ge 2) { @('business.dump', 'accounting.dump', 'workspace.tar.gz') } else { @('business.dump', 'accounting.dump', 'sources.tar.gz') }
        if ($manifest.format -eq 3) { $fileNames += 'extensions.tar.gz' }
        foreach ($name in $fileNames) {
            $actual = (Get-FileHash -Algorithm SHA256 -LiteralPath (Join-Path $BackupFolder $name)).Hash.ToLowerInvariant()
            if ($actual -ne $manifest.files.$name) { throw "Backup checksum mismatch: $name" }
        }
        if (-not $RestoreDatabase) { $RestoreDatabase = "knowledgeops_restore_$backupId" }
        if ($Runtime -eq 'deployment' -and -not $RestoreVolume) { $RestoreVolume = "knowledgeops-agent-restore-$backupId" }
        $accountingTarget = "${RestoreDatabase}_accounting"
        # 不使用 IF NOT EXISTS。碰到已有 DB/卷即拒绝，绝不清空后继续。
        if ($Runtime -eq 'deployment') {
            $existing = @(docker volume ls --format '{{.Name}}')
            if ($existing -contains $RestoreVolume) { throw 'Restore volume already exists.' }
        }
        docker compose exec -T postgres psql -v ON_ERROR_STOP=1 -U knowledgeops -d postgres -c "CREATE DATABASE $RestoreDatabase OWNER knowledgeops"
        docker compose exec -T postgres psql -v ON_ERROR_STOP=1 -U knowledgeops -d postgres -c "CREATE DATABASE $accountingTarget OWNER knowledgeops"
        if ($Runtime -eq 'deployment') { docker volume create $RestoreVolume | Out-Null }
        docker cp (Join-Path $BackupFolder 'business.dump') "${pg}:/tmp/$backupId-restore-business.dump"
        docker cp (Join-Path $BackupFolder 'accounting.dump') "${pg}:/tmp/$backupId-restore-accounting.dump"
        docker compose exec -T postgres pg_restore --exit-on-error --no-owner -U knowledgeops -d $RestoreDatabase "/tmp/$backupId-restore-business.dump"
        docker compose exec -T postgres pg_restore --exit-on-error --no-owner -U knowledgeops -d $accountingTarget "/tmp/$backupId-restore-accounting.dump"
        if ($Runtime -eq 'development') {
            $restoreRoot = "/workspace/artifacts/verification/restores/$RestoreDatabase"
            $arguments = @('scripts/backup_fixture.py', 'verify', '--database', $RestoreDatabase, '--accounting-database', $accountingTarget,
                '--source-root', "$restoreRoot/uploads/originals", '--backup-manifest', "$backupInDev/manifest.json", '--output', "$backupInDev/restore-verification.json")
            if ($manifest.format -ge 2) {
                $arguments += @('--workspace-root', $restoreRoot, '--workspace-archive', "$backupInDev/workspace.tar.gz")
            } else { $arguments += @('--archive', "$backupInDev/sources.tar.gz") }
            docker exec -w /workspace -e MODEL_CALLS_ENABLED=false $dev .venv/bin/python scripts/dev.py --run .venv/bin/python @arguments
        } elseif ($manifest.format -ge 2) {
            docker compose run --rm --no-deps -T -e MODEL_CALLS_ENABLED=false -v "${BackupFolder}:/backup" -v "${RestoreVolume}:/restore-workspace" api python scripts/backup_fixture.py verify --database $RestoreDatabase --accounting-database $accountingTarget --source-root /restore-workspace/uploads/originals --workspace-root /restore-workspace --workspace-archive "/backup/workspace.tar.gz" --backup-manifest "/backup/manifest.json" --output "/backup/restore-verification.json"
        } else {
            docker compose run --rm --no-deps -T -e MODEL_CALLS_ENABLED=false -v "${BackupFolder}:/backup" -v "${RestoreVolume}:/restore-sources" api python scripts/backup_fixture.py verify --database $RestoreDatabase --accounting-database $accountingTarget --source-root /restore-sources --archive "/backup/sources.tar.gz" --backup-manifest "/backup/manifest.json" --output "/backup/restore-verification.json"
        }
        Write-Output "Restored into $RestoreDatabase; active application configuration was not changed."
    }
} finally { Pop-Location }
