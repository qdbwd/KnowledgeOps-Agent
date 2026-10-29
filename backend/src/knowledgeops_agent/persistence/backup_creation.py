"""整组备份的文件快照、操作记录与最终发布。PG归档由宿主CLI调用pg_dump生成。

在全过程维护租约内先记录源文件集合，打包后核对归档内容与源集合，最后才
发布manifest。任何失败都留下独立日志和部分归档，不把残留目录当作成功。
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tarfile
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from typing import IO
from uuid import uuid4

from sqlalchemy.orm import sessionmaker

from knowledgeops_agent.persistence.backup import file_hash
from knowledgeops_agent.persistence.extensions import create_extensions, extension_inventory
from knowledgeops_agent.persistence.maintenance import (
    LOCK_FILE,
    MaintenanceBusy,
    MaintenanceLease,
    MaintenanceLost,
)


class BackupInvalid(ValueError):
    """只携带本模块生成的可展示原因，不透传数据库驱动、连接串或秘密。"""


def atomic_json(path: Path, value: dict) -> None:
    """同目录原子替换，仅用于本次操作记录/新清单，不改写旧备份内容。"""
    temporary = path.with_name(f'.{path.name}.{uuid4().hex}.tmp')
    try:
        with temporary.open('x', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        temporary.unlink(missing_ok=True)


def stream_hash(stream: IO[bytes]) -> str:
    digest = hashlib.sha256()
    while block := stream.read(1024 * 1024):
        digest.update(block)
    return digest.hexdigest()


def tree_inventory(root: Path) -> dict:
    """不跟随链接，完整包含普通文件/空目录/安全的相对链接；不归档维护锁。"""
    root = root.absolute()
    if not root.is_dir() or any(path.is_symlink() for path in (root, *root.parents)):
        raise BackupInvalid('备份工作区必须是实际目录，不能经过链接')
    result = {}
    for current, directories, files in os.walk(root, followlinks=False):
        directories.sort()
        for name in sorted([*directories, *files]):
            path = Path(current) / name
            relative = path.relative_to(root).as_posix()
            if relative == LOCK_FILE:
                continue
            info = path.lstat()
            entry: dict = {'mode': stat.S_IMODE(info.st_mode), 'mtime_ns': info.st_mtime_ns}
            if stat.S_ISLNK(info.st_mode):
                target = os.readlink(path)
                if Path(target).is_absolute() or not path.resolve().is_relative_to(root):
                    raise BackupInvalid('工作区包含指向外部的链接，不能生成不可恢复的备份')
                entry.update(kind='link', target=target)
            elif stat.S_ISDIR(info.st_mode):
                entry.update(kind='directory')
            elif stat.S_ISREG(info.st_mode):
                descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                with os.fdopen(descriptor, 'rb') as stream:
                    before = os.fstat(stream.fileno())
                    digest = hashlib.file_digest(stream, 'sha256').hexdigest()
                    after = os.fstat(stream.fileno())
                if ((info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
                        != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
                        or (before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                        != (after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
                    raise BackupInvalid('备份读取期间工作区文件变化，请停止外部编辑后重试')
                entry.update(kind='file', sha256=digest, size=info.st_size)
            else:
                raise BackupInvalid('工作区存在特殊文件，不能将其作为普通材料归档')
            result[relative] = entry
    return result


def verify_tree_archive(archive: Path, expected: dict) -> None:
    seen = set()
    with tarfile.open(archive, 'r:gz') as packed:
        for member in packed:
            if member.name in seen or member.name not in expected:
                raise BackupInvalid('工作区归档包含重复或未知成员')
            seen.add(member.name)
            entry = expected[member.name]
            if member.mode != entry['mode']:
                raise BackupInvalid('工作区归档权限与源文件不符')
            if entry['kind'] == 'file':
                if not member.isfile() or member.size != entry['size']:
                    raise BackupInvalid('工作区归档文件类型或长度不符')
                stream = packed.extractfile(member)
                if stream is None:
                    raise BackupInvalid('工作区归档文件不可读')
                with stream:
                    if stream_hash(stream) != entry['sha256']:
                        raise BackupInvalid('工作区归档正文与源文件摘要不符')
            elif entry['kind'] == 'link':
                if not member.issym() or member.linkname != entry['target']:
                    raise BackupInvalid('工作区归档链接与源文件不符')
            elif not member.isdir():
                raise BackupInvalid('工作区归档目录类型不符')
    if seen != set(expected):
        raise BackupInvalid('工作区归档缺少源文件')


class BackupCreation:
    def __init__(self, engine, *, folder: Path, workspace: Path, skills: Path, mcp: Path,
                 include_data=False, operation_id: str | None = None, legacy_sources=False,
                 check_clients=False):
        self.engine, self.folder = engine, folder.absolute()
        self.workspace, self.skills, self.mcp = workspace.absolute(), skills, mcp
        self.include_data, self.legacy_sources = include_data, legacy_sources
        self.check_clients = check_clients
        self.operation_id = operation_id or uuid4().hex
        self.lease: MaintenanceLease | None = None
        self._journal: dict = {}
        self._mutex = RLock()
        self._published = False
        self._closed = False
        self._tree: dict = {}
        self._extensions: dict = {}
        self.archive_name = 'sources.tar.gz' if legacy_sources else 'workspace.tar.gz'

    def record(self, **updates):
        with self._mutex:
            self._journal.update(updates, updated_at=datetime.now(UTC).isoformat())
            atomic_json(self.folder / 'operation.json', self._journal)

    def __enter__(self):
        # 宿主需要先创建目录才能挂载；仅允许接续同一新操作的初始记录。
        if self.folder.exists():
            if set(path.name for path in self.folder.iterdir()) != {'operation.json'}:
                raise FileExistsError('必须使用新的备份目录，不能覆盖已有归档')
            initial = json.loads((self.folder / 'operation.json').read_text(encoding='utf-8-sig'))
            if initial.get('id') != self.operation_id or initial.get('state') != 'starting':
                raise FileExistsError('已有备份操作不能原地重新执行')
        else:
            self.folder.mkdir(parents=True)
        self._journal = {'id': self.operation_id, 'kind': 'create', 'state': 'working',
                         'phase': 'acquiring_protection', 'source_database': self.engine.url.database,
                         'started_at': datetime.now(UTC).isoformat()}
        self.record()
        try:
            # 格式1只供隔离旧来源卷演练；只读旧卷没有应用工作区锁载体。
            self.lease = MaintenanceLease(self.engine, workspace=None if self.legacy_sources else self.workspace,
                                          exclusive=True).acquire()
            if self.check_clients:
                from sqlalchemy import text

                # 首次升级时旧进程还不识别新锁，不能仅靠取得锁就宣称已停写。
                assert self.lease.connection is not None
                clients = self.lease.connection.scalar(text(
                    "SELECT count(*) FROM pg_stat_activity WHERE datname IN (:business, :ledger) "
                    "AND backend_type = 'client backend' AND pid <> pg_backend_pid()"
                ), {'business': self.engine.url.database,
                    'ledger': f'{self.engine.url.database}_accounting'})
                if clients:
                    raise BackupInvalid('仍有业务/回执数据库连接，请停止应用和维护命令后重试')
            self.record(phase='capturing_source_set')
            self._tree = tree_inventory(self.workspace)
            if not self.legacy_sources:
                self._extensions, _ = extension_inventory(sessionmaker(self.engine), skills=self.skills,
                                                         mcp=self.mcp, include_data=self.include_data)
            self.check()
            self.record(phase='database_archives')
            return self
        except BaseException as error:
            self.__exit__(type(error), error, error.__traceback__)
            raise

    def check(self):
        if self.lease is None or self._closed:
            raise RuntimeError('备份维护保护未建立或已关闭')
        self.lease.check()

    def archive_files(self):
        self.check()
        self.record(phase='file_archives')
        with tarfile.open(self.folder / self.archive_name, 'x:gz', dereference=False) as packed:
            for name, value in self._tree.items():
                self.check()
                path = self.workspace / name
                entry = tarfile.TarInfo(name)
                entry.mode = value['mode']
                entry.mtime = value['mtime_ns'] / 1_000_000_000
                if value['kind'] == 'directory':
                    entry.type = tarfile.DIRTYPE
                    packed.addfile(entry)
                elif value['kind'] == 'link':
                    entry.type, entry.linkname = tarfile.SYMTYPE, value['target']
                    packed.addfile(entry)
                else:
                    entry.size = value['size']
                    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                    with os.fdopen(descriptor, 'rb') as stream:
                        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                            raise BackupInvalid('工作区文件类型在打包期间变化')
                        packed.addfile(entry, stream)
        if not self.legacy_sources:
            create_extensions(sessionmaker(self.engine), skills=self.skills, mcp=self.mcp,
                              output=self.folder / 'extensions.tar.gz', include_data=self.include_data)
        self.check()
        self.record(phase='verifying_archives')

    def publish(self, checked_databases: dict):
        """接收CLI用pg_restore完整解码校验时绑定的两个hash，不能仅以文件存在发布。"""
        self.check()
        if set(checked_databases) != {'business.dump', 'accounting.dump'}:
            raise BackupInvalid('两份数据库归档必须先完成pg_restore读取校验')
        for name, digest in checked_databases.items():
            if file_hash(self.folder / name) != digest:
                raise BackupInvalid('数据库归档在校验后发生变化')
        verify_tree_archive(self.folder / self.archive_name, self._tree)
        if tree_inventory(self.workspace) != self._tree:
            raise BackupInvalid('工作区在备份期间发生变化，请停止外部编辑并重新创建备份')
        files = [*checked_databases, self.archive_name]
        if not self.legacy_sources:
            latest, _ = extension_inventory(sessionmaker(self.engine), skills=self.skills, mcp=self.mcp,
                                            include_data=self.include_data)
            if latest != self._extensions:
                raise BackupInvalid('扩展在备份期间发生变化，请停止外部编辑并重新创建备份')
            with tarfile.open(self.folder / 'extensions.tar.gz', 'r:gz') as archive:
                members = archive.getmembers()
                names = [member.name for member in members]
                if (len(set(names)) != len(names) or set(names) != {'extensions.json', *self._extensions['files']}
                        or any(not member.isfile() for member in members)):
                    raise BackupInvalid('扩展归档包含多余、重复或非普通文件成员')
                stream = archive.extractfile('extensions.json')
                if stream is None:
                    raise BackupInvalid('扩展归档缺少清单')
                with stream:
                    packed_manifest = json.load(stream)
                if packed_manifest != self._extensions:
                    raise BackupInvalid('扩展归档清单与同批源集合不符')
                for name, digest in self._extensions['files'].items():
                    item = archive.extractfile(name)
                    if item is None:
                        raise BackupInvalid('扩展归档缺少文件')
                    with item:
                        actual = stream_hash(item)
                    if actual != digest:
                        raise BackupInvalid('扩展归档文件损坏')
            files.append('extensions.tar.gz')
        manifest = {'format': 1 if self.legacy_sources else 3, 'operation_id': self.operation_id,
                    'created_at': datetime.now(UTC).isoformat(), 'source_database': self.engine.url.database,
                    'source_accounting_database': f'{self.engine.url.database}_accounting',
                    'files': {name: file_hash(self.folder / name) for name in files},
                    'mcp_data_included': self.include_data,
                    'extensions_policy': 'Complete skills and MCP locks; no dependency environments, caches or project secrets.',
                    'accounting_policy': 'Restore only into fresh isolated databases; never overwrite active budgets or receipts.',
                    'consistency': 'Application write/backup exclusion held; source and archive contents matched before publication.'}
        self.check()
        atomic_json(self.folder / 'manifest.json', manifest)
        self._published = True
        self.check()
        self.record(state='created', phase='complete', integrity_verified=True,
                    manifest_sha256=file_hash(self.folder / 'manifest.json'))

    def __exit__(self, kind, error, traceback):
        try:
            if kind is None:
                self.check()
                if not self._published:
                    raise RuntimeError('备份命令结束但未发布完整归档')
            else:
                raise error
        except BaseException as failure:
            # 只撤销本次刚发布的清单；旧备份从未进入此对象，也不删除部分归档。
            if self._published and (self.folder / 'manifest.json').exists():
                os.replace(self.folder / 'manifest.json', self.folder / 'manifest.incomplete.json')
            reason = str(failure) if isinstance(failure, (BackupInvalid, MaintenanceBusy, MaintenanceLost)) else '备份操作被中断或归档读写失败'
            self.record(state='failed', error_code=type(failure).__name__,
                        message=reason + '。保留部分归档与记录，排除原因后使用新目录重试。')
            if kind is None:
                raise
        finally:
            self._closed = True
            if self.lease is not None:
                self.lease.close()
        return False
