<#
本机 Docker 维护入口。备份业务库、独立费用账本与不可变原件。
restore 仅允许新建的 knowledgeops_restore_* 空数据库和全新来源卷；不覆盖
正常业务库、不回滚当前费用账本、不删除卷。业务恢复后仍需启动恢复校验。
#>
[CmdletBinding()]
param(
    [ValidateSet('create', 'restore')][string]$Mode = 'create',
    [string]$BackupFolder,
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
    if ($Mode -eq 'create') {
        if (Test-Path -LiteralPath $BackupFolder) { throw 'Use a new backup folder; existing backups are immutable.' }
        New-Item -ItemType Directory -Path $BackupFolder | Out-Null
        docker compose exec -T postgres pg_dump -U knowledgeops -d $Database -Fc --file="/tmp/$backupId-business.dump"
        docker compose exec -T postgres pg_dump -U knowledgeops -d knowledgeops_accounting -Fc --file="/tmp/$backupId-accounting.dump"
        docker cp "${pg}:/tmp/$backupId-business.dump" (Join-Path $BackupFolder 'business.dump')
        docker cp "${pg}:/tmp/$backupId-accounting.dump" (Join-Path $BackupFolder 'accounting.dump')
        if ($SourceVolume) {
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
        $hashes = @{}
        foreach ($name in $fileNames) {
            $hashes[$name] = (Get-FileHash -Algorithm SHA256 -LiteralPath (Join-Path $BackupFolder $name)).Hash.ToLowerInvariant()
        }
        @{
            format = $format; created_at = [DateTime]::UtcNow.ToString('o'); source_database = $Database
            files = $hashes
            accounting_policy = 'Restore only into a fresh isolated database; never overwrite active task budgets or model replay receipts.'
            consistency = 'Stop api/worker and local file editors before backup. PG pointers and the complete workspace form one backup set; immutable files may include harmless orphans.'
        } | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $BackupFolder 'manifest.json') -Encoding utf8
        Write-Output "Backup: $BackupFolder"
    } else {
        $manifest = Get-Content -Raw -LiteralPath (Join-Path $BackupFolder 'manifest.json') | ConvertFrom-Json
        if ($manifest.format -notin @(1, 2)) { throw 'Unsupported backup format.' }
        $fileNames = if ($manifest.format -eq 2) { @('business.dump', 'accounting.dump', 'workspace.tar.gz') } else { @('business.dump', 'accounting.dump', 'sources.tar.gz') }
        foreach ($name in $fileNames) {
            $actual = (Get-FileHash -Algorithm SHA256 -LiteralPath (Join-Path $BackupFolder $name)).Hash.ToLowerInvariant()
            if ($actual -ne $manifest.files.$name) { throw "Backup checksum mismatch: $name" }
        }
        if (-not $RestoreDatabase) { $RestoreDatabase = "knowledgeops_restore_$backupId" }
        if (-not $RestoreVolume) { $RestoreVolume = "knowledgeops-agent-restore-$backupId" }
        $accountingTarget = "${RestoreDatabase}_accounting"
        # 不使用 IF NOT EXISTS。碰到已有 DB/卷即拒绝，绝不清空后继续。
        $existing = @(docker volume ls --format '{{.Name}}')
        if ($existing -contains $RestoreVolume) { throw 'Restore volume already exists.' }
        docker compose exec -T postgres psql -v ON_ERROR_STOP=1 -U knowledgeops -d postgres -c "CREATE DATABASE $RestoreDatabase OWNER knowledgeops"
        docker compose exec -T postgres psql -v ON_ERROR_STOP=1 -U knowledgeops -d postgres -c "CREATE DATABASE $accountingTarget OWNER knowledgeops"
        docker volume create $RestoreVolume | Out-Null
        docker cp (Join-Path $BackupFolder 'business.dump') "${pg}:/tmp/$backupId-restore-business.dump"
        docker cp (Join-Path $BackupFolder 'accounting.dump') "${pg}:/tmp/$backupId-restore-accounting.dump"
        docker compose exec -T postgres pg_restore --exit-on-error --no-owner -U knowledgeops -d $RestoreDatabase "/tmp/$backupId-restore-business.dump"
        docker compose exec -T postgres pg_restore --exit-on-error --no-owner -U knowledgeops -d $accountingTarget "/tmp/$backupId-restore-accounting.dump"
        if ($manifest.format -eq 2) {
            docker compose run --rm --no-deps -T -e MODEL_CALLS_ENABLED=false -v "${BackupFolder}:/backup" -v "${RestoreVolume}:/restore-workspace" api python scripts/backup_fixture.py verify --database $RestoreDatabase --accounting-database $accountingTarget --source-root /restore-workspace/uploads/originals --workspace-root /restore-workspace --workspace-archive "/backup/workspace.tar.gz" --output "/backup/restore-verification.json"
        } else {
            docker compose run --rm --no-deps -T -e MODEL_CALLS_ENABLED=false -v "${BackupFolder}:/backup" -v "${RestoreVolume}:/restore-sources" api python scripts/backup_fixture.py verify --database $RestoreDatabase --accounting-database $accountingTarget --source-root /restore-sources --archive "/backup/sources.tar.gz" --output "/backup/restore-verification.json"
        }
        Write-Output "Restored into $RestoreDatabase and $RestoreVolume; active application configuration was not changed."
    }
} finally { Pop-Location }
