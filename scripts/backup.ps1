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
$guard = $null
$createdFolder = $false
$restoreRecordPath = $null
$restoreRecord = $null
if (-not $BackupFolder) { $BackupFolder = Join-Path $backupBase $backupId }
$BackupFolder = [IO.Path]::GetFullPath($BackupFolder)
if (-not $BackupFolder.StartsWith($backupBase + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'Backup folder must be inside this project artifacts/backups.'
}
Push-Location $projectRoot

function Read-GuardReply {
    param($Process)
    $pendingLine = $Process.StandardOutput.ReadLineAsync()
    $deadline = [DateTime]::UtcNow.AddMinutes(15)
    while (-not $pendingLine.Wait(1000)) {
        if ([DateTime]::UtcNow -gt $deadline) { throw 'Backup protection command timed out; partial output is not a backup.' }
    }
    $line = $pendingLine.GetAwaiter().GetResult()
    if (-not $line) { throw 'Backup protection process stopped before confirmation.' }
    $reply = $line | ConvertFrom-Json
    if (-not $reply.ok) { throw "Backup protection failed ($($reply.code)); inspect operation.json and use a new folder after correcting the cause." }
    return $reply
}

function Send-GuardCommand {
    param($Process, [hashtable]$Message)
    if ($Process.HasExited) { throw 'Backup protection process is no longer running.' }
    $Process.StandardInput.WriteLine(($Message | ConvertTo-Json -Compress -Depth 6))
    $Process.StandardInput.Flush()
    return Read-GuardReply $Process
}

function Write-OperationRecord {
    param([string]$Path, [hashtable]$Record)
    $Record.updated_at = [DateTime]::UtcNow.ToString('o')
    $temporary = "$Path.tmp"
    $Record | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $temporary -Encoding utf8
    Move-Item -LiteralPath $temporary -Destination $Path -Force
}

try {
    if ($Mode -eq 'create') {
        if (Test-Path -LiteralPath $BackupFolder) { throw 'Use a new backup folder; existing backups are immutable.' }
        New-Item -ItemType Directory -Path $BackupFolder | Out-Null
        $createdFolder = $true
        @{ id = $backupId; kind = 'create'; state = 'starting'; phase = 'preflight'; source_database = $Database
            started_at = [DateTime]::UtcNow.ToString('o'); updated_at = [DateTime]::UtcNow.ToString('o')
        } | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $BackupFolder 'operation.json') -Encoding utf8
    } else {
        if (-not (Test-Path -LiteralPath (Join-Path $BackupFolder 'manifest.json') -PathType Leaf)) {
            throw 'A completed backup manifest is required; partial folders cannot be restored.'
        }
        $operationsFolder = Join-Path $BackupFolder 'operations'
        New-Item -ItemType Directory -Path $operationsFolder -Force | Out-Null
        $restoreRecordPath = Join-Path $operationsFolder "$backupId.json"
        if (Test-Path -LiteralPath $restoreRecordPath) { throw 'Restore operation identity already exists.' }
        $restoreRecord = @{id=$backupId; kind='restore'; state='working'; phase='preflight';
                          started_at=[DateTime]::UtcNow.ToString('o')}
        Write-OperationRecord $restoreRecordPath $restoreRecord
    }
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
        if ($SourceVolume -and $Database -eq 'knowledgeops') { throw 'A normal backup must include the complete workspace, not a fixture source volume.' }
        $running = @(docker compose -f compose.yaml -f compose.dev.yaml --profile development ps --status running --format '{{.Service}}')
        if ($running | Where-Object { $_ -in @('api', 'worker', 'init') }) {
            throw 'Stop this project api, worker and init processes before backup. The script does not stop them automatically.'
        }
        # 长连接进程先取得维护互斥，再允许任何数据库/文件导出。stdin关闭即失败。
        $helperArgs = @('scripts/backup_guard.py', '--database', $Database, '--operation-id', $backupId)
        if ($IncludeMcpData) { $helperArgs += '--include-data' }
        if ($Runtime -eq 'development') {
            $helperArgs += @('--folder', $backupInDev)
            if ($WorkspacePath) {
                $helperArgs += @('--workspace', $WorkspacePath, '--fixture-root', ($WorkspacePath -replace '/workspace$', '/extensions'))
            }
            $dockerArgs = @('exec', '-i', '-w', '/workspace', '-e', 'MODEL_CALLS_ENABLED=false', $dev,
                '.venv/bin/python', 'scripts/dev.py', '--run', '.venv/bin/python') + $helperArgs
        } else {
            $helperArgs += @('--folder', '/backup')
            $dockerArgs = @('compose', 'run', '--rm', '--no-deps', '-T', '-e', 'MODEL_CALLS_ENABLED=false', '-v', "${BackupFolder}:/backup")
            if ($SourceVolume) {
                $dockerArgs += @('-v', "${SourceVolume}:/backup-sources:ro")
                $helperArgs += @('--workspace', '/backup-sources', '--legacy-sources')
            }
            $dockerArgs += @('api', 'python') + $helperArgs
        }
        $start = [System.Diagnostics.ProcessStartInfo]::new('docker')
        $start.WorkingDirectory = $projectRoot
        $start.UseShellExecute = $false
        $start.CreateNoWindow = $true
        $start.WindowStyle = [System.Diagnostics.ProcessWindowStyle]::Hidden
        $start.RedirectStandardInput = $true
        $start.RedirectStandardOutput = $true
        $start.RedirectStandardError = $true
        $start.StandardInputEncoding = [Text.UTF8Encoding]::new($false)
        $start.StandardOutputEncoding = [Text.UTF8Encoding]::new($false)
        foreach ($argument in $dockerArgs) { $start.ArgumentList.Add($argument) }
        $guard = [System.Diagnostics.Process]::Start($start)
        $guardErrors = $guard.StandardError.ReadToEndAsync()
        $ready = Read-GuardReply $guard
        if ($ready.phase -ne 'ready' -or $ready.protocol -ne 1) { throw 'Unexpected backup protection protocol.' }
        Write-Output 'Write protection acquired; exporting the business and accounting databases.'
        docker compose exec -T postgres pg_dump -U knowledgeops -d $Database -Fc --file="/tmp/$backupId-business.dump"
        # 测试种子必须使用自己的回执库，不复制正式任务预算来伪装隔离验证。
        $accountingSource = "${Database}_accounting"
        docker compose exec -T postgres pg_dump -U knowledgeops -d $accountingSource -Fc --file="/tmp/$backupId-accounting.dump"
        docker cp "${pg}:/tmp/$backupId-business.dump" (Join-Path $BackupFolder 'business.dump')
        docker cp "${pg}:/tmp/$backupId-accounting.dump" (Join-Path $BackupFolder 'accounting.dump')
        Send-GuardCommand $guard @{command = 'check'} | Out-Null
        # 完整读取压缩归档，而不是只看目录。这里不连接目标库、不执行SQL。
        $checked = @{}
        foreach ($name in @('business.dump', 'accounting.dump')) {
            $remotePath = "/tmp/$backupId-$name"
            $before = ((docker exec $pg sha256sum $remotePath) -split '\s+')[0]
            docker compose exec -T postgres pg_restore --exit-on-error --file=/dev/null $remotePath
            $after = ((docker exec $pg sha256sum $remotePath) -split '\s+')[0]
            $localHash = (Get-FileHash -Algorithm SHA256 -LiteralPath (Join-Path $BackupFolder $name)).Hash.ToLowerInvariant()
            if ($before -ne $after -or $before -ne $localHash) { throw 'Database archive changed while checking or copying.' }
            $checked[$name] = $localHash
        }
        Write-Output 'Archiving and checking the complete file set under the same protection.'
        Send-GuardCommand $guard @{command = 'archive'} | Out-Null
        Send-GuardCommand $guard @{command = 'publish'; checked_databases = $checked} | Out-Null
        $guard.StandardInput.Close()
        if (-not $guard.WaitForExit(10000) -or $guard.ExitCode -ne 0) { throw 'Backup helper did not complete cleanly; inspect operation.json.' }
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
        $restoreRecord.phase = 'restoring_databases'
        $restoreRecord.database = $RestoreDatabase
        $restoreRecord.accounting_database = $accountingTarget
        Write-OperationRecord $restoreRecordPath $restoreRecord
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
        $restoreRecord.phase = 'verifying_restored_data'
        $restoreReportName = "restore-verification-$backupId.json"
        $restoreRecord.report = $restoreReportName
        Write-OperationRecord $restoreRecordPath $restoreRecord
        if ($Runtime -eq 'development') {
            $restoreRoot = "/workspace/artifacts/verification/restores/$RestoreDatabase"
            $arguments = @('scripts/backup_fixture.py', 'verify', '--database', $RestoreDatabase, '--accounting-database', $accountingTarget,
                '--source-root', "$restoreRoot/uploads/originals", '--backup-manifest', "$backupInDev/manifest.json", '--output', "$backupInDev/$restoreReportName")
            if ($manifest.format -ge 2) {
                $arguments += @('--workspace-root', $restoreRoot, '--workspace-archive', "$backupInDev/workspace.tar.gz")
            } else { $arguments += @('--archive', "$backupInDev/sources.tar.gz") }
            docker exec -w /workspace -e MODEL_CALLS_ENABLED=false $dev .venv/bin/python scripts/dev.py --run .venv/bin/python @arguments
        } elseif ($manifest.format -ge 2) {
            docker compose run --rm --no-deps -T -e MODEL_CALLS_ENABLED=false -v "${BackupFolder}:/backup" -v "${RestoreVolume}:/restore-workspace" api python scripts/backup_fixture.py verify --database $RestoreDatabase --accounting-database $accountingTarget --source-root /restore-workspace/uploads/originals --workspace-root /restore-workspace --workspace-archive "/backup/workspace.tar.gz" --backup-manifest "/backup/manifest.json" --output "/backup/$restoreReportName"
        } else {
            docker compose run --rm --no-deps -T -e MODEL_CALLS_ENABLED=false -v "${BackupFolder}:/backup" -v "${RestoreVolume}:/restore-sources" api python scripts/backup_fixture.py verify --database $RestoreDatabase --accounting-database $accountingTarget --source-root /restore-sources --archive "/backup/sources.tar.gz" --backup-manifest "/backup/manifest.json" --output "/backup/$restoreReportName"
        }
        $verifiedPath = Join-Path $BackupFolder $restoreReportName
        $verified = Get-Content -Raw -LiteralPath $verifiedPath | ConvertFrom-Json
        $manifestHash = (Get-FileHash -Algorithm SHA256 -LiteralPath (Join-Path $BackupFolder 'manifest.json')).Hash.ToLowerInvariant()
        if ($verified.backup_manifest_sha256 -ne $manifestHash -or $verified.database -ne $RestoreDatabase -or
            -not $verified.checks -or @($verified.checks.PSObject.Properties | Where-Object { $_.Value -ne $true }).Count) {
            throw 'Restore verification did not prove this backup and isolated target.'
        }
        # 保留每次恢复报告。兼容旧调用者的固定路径只是最新成功报告，替换前保存原字节。
        $latest = Join-Path $BackupFolder 'restore-verification.json'
        if (Test-Path -LiteralPath $latest) {
            $priorHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $latest).Hash.ToLowerInvariant()
            $priorCopy = Join-Path $operationsFolder "restore-verification-$priorHash.json"
            if (-not (Test-Path -LiteralPath $priorCopy)) { Copy-Item -LiteralPath $latest -Destination $priorCopy }
        }
        $latestTemporary = Join-Path $BackupFolder "restore-latest-$backupId.tmp"
        Copy-Item -LiteralPath $verifiedPath -Destination $latestTemporary
        Move-Item -LiteralPath $latestTemporary -Destination $latest -Force
        $restoreRecord.state = 'validated'
        $restoreRecord.phase = 'complete'
        $restoreRecord.backup_manifest_sha256 = $manifestHash
        Write-OperationRecord $restoreRecordPath $restoreRecord
        Write-Output "Restored into $RestoreDatabase; active application configuration was not changed."
    }
} catch {
    if ($restoreRecordPath) {
        $restoreRecord.state = 'failed'
        $restoreRecord.error_code = 'RestoreCommandFailed'
        $restoreRecord.message = 'Restore failed. The original backup and isolated partial target are preserved; retry with a new target after checking this phase.'
        Write-OperationRecord $restoreRecordPath $restoreRecord
    }
    if ($createdFolder) {
        $operationPath = Join-Path $BackupFolder 'operation.json'
        $record = Get-Content -Raw -LiteralPath $operationPath | ConvertFrom-Json -AsHashtable
        if ($record.state -notin @('created', 'failed')) {
            $record.state = 'failed'
            $record.error_code = 'HostCommandFailed'
            $record.message = 'The backup command failed. Keep this folder for diagnosis and retry with a new folder.'
            $record.updated_at = [DateTime]::UtcNow.ToString('o')
            $temporaryRecord = Join-Path $BackupFolder 'operation.host.tmp'
            $record | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $temporaryRecord -Encoding utf8
            Move-Item -LiteralPath $temporaryRecord -Destination $operationPath -Force
        }
    }
    throw
} finally {
    if ($guard) {
        try {
            if (-not $guard.HasExited) {
                $guard.StandardInput.Close()
                if (-not $guard.WaitForExit(10000)) { $guard.Kill($true) }
            }
        } finally { $guard.Dispose() }
    }
    Pop-Location
}
