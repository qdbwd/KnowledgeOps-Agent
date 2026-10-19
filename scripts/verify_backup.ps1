<#
真实 pg_dump/pg_restore 演练：0030旧表、当前完整资产，以及显式去掉HTML之后的备份。
只创建随机backupfixture/restore数据库，运行当前dev源码且关闭模型。默认收尾删除本脚本
创建的隔离数据库；证据和归档留在artifacts。绝不连接正式库做迁移/清理。
#>
[CmdletBinding()]
param(
    [switch]$KeepDatabases,
    [ValidateSet('all', 'legacy', 'current')][string]$Scenario = 'all'
)
$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $true
$projectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$backupTool = Join-Path $PSScriptRoot 'backup.ps1'
$created = [Collections.Generic.List[string]]::new()
$reports = [Collections.Generic.List[object]]::new()
$exerciseId = [guid]::NewGuid().ToString('N').Substring(0, 12)
$output = Join-Path $projectRoot "artifacts/verification/increment-backup-$exerciseId.json"

function New-Database([string]$Name) {
    if ($Name -notmatch '^knowledgeops_backupfixture_[a-f0-9]{12}(?:_accounting)?$') { throw 'Invalid fixture database.' }
    docker exec $pg psql -v ON_ERROR_STOP=1 -U knowledgeops -d postgres -c "CREATE DATABASE $Name OWNER knowledgeops"
    $created.Add($Name)
}

function Run-Fixture([string]$Action, [string]$Database, [string]$Workspace, [string[]]$Extra = @()) {
    docker exec -w /workspace -e MODEL_CALLS_ENABLED=false $dev .venv/bin/python scripts/dev.py --run .venv/bin/python scripts/backup_fixture.py $Action --database $Database --source-root "$Workspace/uploads/originals" --workspace-root $Workspace @Extra
}

function Restore-And-Read([string]$Backup) {
    $target = 'knowledgeops_restore_' + [guid]::NewGuid().ToString('N').Substring(0, 12)
    # 先确认名字不存在才加入本次收尾名单，失败不能误删其他人的隔离库。
    $exists = docker exec $pg psql -At -U knowledgeops -d postgres -c "SELECT datname FROM pg_database WHERE datname IN ('$target','${target}_accounting')"
    if ($exists) { throw 'Random restore identity already exists.' }
    $created.Add($target)
    $created.Add("${target}_accounting")
    & $backupTool -Mode restore -Runtime development -BackupFolder $Backup -RestoreDatabase $target | Out-Host
    $result = Get-Content -Raw -LiteralPath (Join-Path $Backup 'restore-verification.json') | ConvertFrom-Json
    if (@($result.checks.PSObject.Properties | Where-Object { $_.Value -ne $true }).Count) { throw 'Restore verification failed.' }
    return $result
}

Push-Location $projectRoot
try {
    $pg = (docker compose ps -q postgres).Trim()
    $dev = (docker compose -f compose.yaml -f compose.dev.yaml ps -q dev).Trim()
    if (-not $pg -or -not $dev) { throw 'Start this project dev and postgres first.' }
    $scenarios = if ($Scenario -eq 'all') { @('legacy', 'current') } else { @($Scenario) }
    foreach ($case in $scenarios) {
        $identity = [guid]::NewGuid().ToString('N').Substring(0, 12)
        $database = "knowledgeops_backupfixture_$identity"
        $workspace = "/workspace/artifacts/verification/backup-$identity/workspace"
        $backup = Join-Path $projectRoot "artifacts/backups/$identity"
        New-Database $database
        New-Database "${database}_accounting"
        $seed = if ($case -eq 'legacy') { 'seed-legacy' } else { 'seed' }
        Run-Fixture $seed $database $workspace
        $previewInDev = "/workspace/artifacts/verification/backup-$identity/preview.json"
        Run-Fixture 'inspect-html' $database $workspace @('--output', $previewInDev)
        $preview = Get-Content -Raw -LiteralPath "artifacts/verification/backup-$identity/preview.json" | ConvertFrom-Json
        if ($preview.items.Count -ne 1 -or -not $preview.items[0].eligible) { throw 'Expected exactly one valid legacy HTML fixture.' }
        & $backupTool -Mode create -Runtime development -Database $database -WorkspacePath $workspace -BackupFolder $backup
        $restored = Restore-And-Read $backup
        $body = @($restored.bodies | Where-Object version_id -eq $preview.items[0].version_id)
        if ($body.Count -ne 1 -or $body[0].sha256 -ne $preview.items[0].body_hash) { throw 'Legacy body changed during restore/migration.' }
        if ($case -eq 'current') {
            if (@($restored.increment_configuration.PSObject.Properties).Count -lt 8 -or @($restored.increment_configuration.PSObject.Properties | Where-Object { $_.Value -ne $true }).Count) {
                throw 'New category/group/role/policy/mode or frozen configuration was not restored exactly.'
            }
            if ($restored.delivered_files.Count -ne 1 -or $restored.checkpoints_found -ne 1 -or $restored.tables.evidence -ne 2 -or $restored.learning_profile_files -lt 4) {
                throw 'Current fixture did not restore the required asset/control coverage.'
            }
            $cleanInDev = "/workspace/artifacts/verification/backup-$identity/cleaned.json"
            Run-Fixture 'clean-html' $database $workspace @('--backup-folder', "/workspace/artifacts/backups/$identity", '--output', $cleanInDev)
            $cleaned = Get-Content -Raw -LiteralPath "artifacts/verification/backup-$identity/cleaned.json" | ConvertFrom-Json
            if ($cleaned.remaining.Count -ne 0 -or $cleaned.jobs.Count -ne 1) { throw 'Explicit fixture HTML cleanup was incomplete.' }
            $afterId = [guid]::NewGuid().ToString('N').Substring(0, 12)
            $afterBackup = Join-Path $projectRoot "artifacts/backups/$afterId"
            & $backupTool -Mode create -Runtime development -Database $database -WorkspacePath $workspace -BackupFolder $afterBackup
            $after = Restore-And-Read $afterBackup
            $beforeBodies = @($restored.bodies | Sort-Object version_id | ForEach-Object { "$($_.version_id):$($_.sha256)" })
            $afterBodies = @($after.bodies | Sort-Object version_id | ForEach-Object { "$($_.version_id):$($_.sha256)" })
            if (Compare-Object $beforeBodies $afterBodies) { throw 'Saved body identities/content changed after HTML cleanup.' }
            if ($after.files.Count -ne $restored.files.Count - 1 -or $after.tables.evidence -ne $restored.tables.evidence -or $after.delivered_files.Count -ne 1 -or $after.tables.cleanup_jobs -lt 1) {
                throw 'After-cleanup backup did not preserve citations/deliveries/jobs or still requires removed HTML.'
            }
            $reports.Add(@{scenario='after-html-cleanup'; backup=$afterBackup; checks=$after.checks; body_hashes_unchanged=$true; html_file_removed=$true})
        }
        $reports.Add(@{scenario=$case; backup=$backup; checks=$restored.checks; legacy_body_unchanged=$true; checkpoints=$restored.checkpoints_found; accounting_migration=$restored.accounting_migration})
    }
    @{exercise=$exerciseId; scenarios=$reports; model_calls=0; formal_database_modified=$false} | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath $output -Encoding utf8
    Write-Host "Backup exercise passed: $output"
} finally {
    if (-not $KeepDatabases) {
        foreach ($name in $created) {
            if ($name -notmatch '^knowledgeops_(?:backupfixture|restore)_[a-f0-9]{12}(?:_accounting)?$') { throw 'Refusing cleanup outside created test databases.' }
            docker exec $pg psql -v ON_ERROR_STOP=1 -U knowledgeops -d postgres -c "DROP DATABASE IF EXISTS $name WITH (FORCE)"
        }
    }
    Pop-Location
}
