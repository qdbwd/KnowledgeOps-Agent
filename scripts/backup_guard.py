"""备份CLI的长连接保护进程。stdin关闭即失败，不依赖易过期的锁文件或成功标记。"""

import argparse
import json
import os
import re
import signal
import sys
from pathlib import Path
from threading import Event, Thread

from sqlalchemy import create_engine
from sqlalchemy.engine import make_url

from knowledgeops_agent.core.config import Settings
from knowledgeops_agent.core.resources import managed_resource_root, resource_path
from knowledgeops_agent.persistence.backup_creation import BackupCreation, BackupInvalid


def active_writers() -> list[dict]:
    """读/proc而非依赖镜像安装ps；只输出进程身份类别，不泄漏命令参数/密钥。"""
    markers = ('knowledgeops_agent.api', 'knowledgeops_agent.worker', 'scripts/bootstrap.sh',
               'scripts/initialize_skills.py', 'scripts/migrate_', 'scripts/reindex_',
               'scripts/maintain_lifecycle.py', 'scripts/retire_web_originals.py', 'scripts/seed_demo.py')
    rows = []
    for path in Path('/proc').glob('[0-9]*/cmdline'):
        try:
            command = path.read_bytes().replace(b'\0', b' ').decode('utf-8', errors='replace')
            if int(path.parent.name) == os.getpid() or 'scripts/backup_guard.py' in command:
                continue
            if (any(marker in command for marker in markers)
                    or ('scripts/dev.py' in command and '--run' not in command)
                    or '/mcp/releases/' in command):
                rows.append({'pid': int(path.parent.name), 'kind': 'application_or_maintenance'})
        except (OSError, ValueError):
            continue  # /proc中的进程在读取间隙正常退出。
    return rows


def reply(**value):
    print(json.dumps(value, ensure_ascii=False), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', required=True)
    parser.add_argument('--folder', type=Path, required=True)
    parser.add_argument('--operation-id', required=True)
    parser.add_argument('--workspace', type=Path)
    parser.add_argument('--fixture-root', type=Path)
    parser.add_argument('--include-data', action='store_true')
    parser.add_argument('--legacy-sources', action='store_true')
    args = parser.parse_args()
    if not re.fullmatch(r'knowledgeops(?:_backupfixture_[a-f0-9]{12})?', args.database):
        parser.error('仅允许本应用及明确的隔离备份库')
    if not re.fullmatch(r'[a-f0-9]{12,32}', args.operation_id):
        parser.error('操作身份无效')
    if args.database == 'knowledgeops' and (args.fixture_root or args.workspace or args.legacy_sources):
        parser.error('正式备份不能通过参数改为部分目录')
    settings = Settings(model_calls_enabled=False)
    workspace = args.workspace or settings.workspace_root
    if workspace is None:
        parser.error('需要实际工作区')
    if args.database != 'knowledgeops' and not args.workspace:
        parser.error('隔离备份必须使用自己的工作区')
    if args.fixture_root and not re.fullmatch(r'/workspace/artifacts/verification/backup-[a-f0-9]{12}/extensions', str(args.fixture_root)):
        parser.error('隔离扩展目录不在约定范围')
    folder = args.folder.absolute()
    if (folder != Path('/backup') and not folder.is_relative_to(Path('/workspace/artifacts/backups'))
            or any(path.is_symlink() for path in (folder, *folder.parents))):
        parser.error('备份目标必须为项目备份目录或明确的备份挂载')
    skills = args.fixture_root / 'skills' if args.fixture_root else resource_path('SKILLS_DIRECTORY', managed_resource_root('skills'))
    mcp = args.fixture_root / 'mcp' if args.fixture_root else resource_path('MCP_DIRECTORY', managed_resource_root('mcp'))
    engine = create_engine(make_url(settings.database_url).set(database=args.database), connect_args={'connect_timeout': 5})
    operation = BackupCreation(engine, folder=folder, workspace=workspace, skills=skills, mcp=mcp,
                               include_data=args.include_data, operation_id=args.operation_id,
                               legacy_sources=args.legacy_sources, check_clients=True)

    def interrupted(*_):
        raise InterruptedError('备份命令被中断')

    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    try:
        with operation:
            if args.database == 'knowledgeops' and active_writers():
                raise BackupInvalid('容器内仍有应用或维护写入进程，请停止后重试')
            stop = Event()
            failed = Event()

            def heartbeat():
                while not stop.wait(1):
                    try:
                        operation.check()
                        operation.record()
                    except Exception:
                        failed.set()
                        return

            watcher = Thread(target=heartbeat, name='backup-protection', daemon=True)
            watcher.start()
            try:
                assert operation.lease is not None
                reply(ok=True, phase='ready', protocol=1, backend_pid=operation.lease.backend_pid)
                complete = False
                for line in sys.stdin:
                    if failed.is_set():
                        raise RuntimeError('备份维护连接丢失')
                    request = json.loads(line)
                    operation.check()
                    command = request.get('command')
                    if command == 'check':
                        reply(ok=True, phase='protected')
                    elif command == 'archive':
                        operation.archive_files()
                        reply(ok=True, phase='archived')
                    elif command == 'publish':
                        operation.publish(request.get('checked_databases', {}))
                        complete = True
                        break
                    else:
                        raise RuntimeError('宿主取消备份或发送未知命令')
                if not complete:
                    raise InterruptedError('宿主连接关闭，备份未发布')
            finally:
                stop.set()
                watcher.join(timeout=5)
        reply(ok=True, phase='complete')
    except Exception as error:
        # 含连接串的底层异常不能流入终端或网页；具体阶段来自operation.json。
        reply(ok=False, code=type(error).__name__, message='备份未完成，请查看阶段记录并检查停写状态、数据库连接及目录；使用新目录重试。')
        raise SystemExit(1) from None
    finally:
        engine.dispose()


if __name__ == '__main__':
    main()
