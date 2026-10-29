"""应用使用者与停写备份的合作式互斥，不依赖迁移后的业务表。

PG会话锁约束同一数据库/schema；Linux文件锁保护完整工作区，即使PG连接
丢失、进程仍在收尾写文件，备份也不能越过该活进程。锁文件只作互斥载体，
不能根据其存在判断应用活跃，不能删除它来“解锁”。不约束外部编辑器。
"""

from __future__ import annotations

import fcntl
import os
import stat
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from threading import RLock

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

LOCK_FILE = '.knowledgeops-maintenance.lock'


class MaintenanceBusy(RuntimeError):
    """已有应用使用者/维护操作，调用者应停止并说明原因，而不是等待后偷偷执行。"""


class MaintenanceLost(RuntimeError):
    """取得锁的原会话或锁文件身份丢失；当前操作不能再报告成功。"""


class MaintenanceLease:
    def __init__(self, engine: Engine, *, workspace: Path | None = None, exclusive=False):
        self.engine, self.exclusive = engine, exclusive
        self.workspace = Path(workspace).absolute() if workspace is not None else None
        self.lock_path = self.workspace / LOCK_FILE if self.workspace else None
        self.connection: Connection | None = None
        self.backend_pid: int | None = None
        self.key: int | None = None
        self._fd: int | None = None
        self._file_identity: tuple[int, int] | None = None
        self._lost = False
        self._mutex = RLock()

    def acquire(self) -> MaintenanceLease:
        """PG先于文件，均非阻塞；任一步失败释放已取得的部分，不留半持有状态。"""
        with self._mutex:
            if self.connection is not None or self._lost:
                raise MaintenanceLost('维护租约不能重复取得或在失效后重新使用')
            try:
                self.connection = self.engine.connect().execution_options(isolation_level='AUTOCOMMIT')
                self.connection.exec_driver_sql("SET statement_timeout = '3s'")
                self.backend_pid = self.connection.scalar(text('SELECT pg_backend_pid()'))
                self.key = self.connection.scalar(text(
                    "SELECT hashtextextended(concat_ws('/', current_database(), current_schema(), 'knowledgeops-maintenance-v1'), 0)"
                ))
                function = 'pg_try_advisory_lock' if self.exclusive else 'pg_try_advisory_lock_shared'
                if not self.connection.scalar(text(f'SELECT {function}(:key)'), {'key': self.key}):
                    raise MaintenanceBusy('应用仍在使用数据库，或另一项备份正在进行；请停止写入后重试')
                if self.workspace is not None:
                    if any(path.is_symlink() for path in (self.workspace, *self.workspace.parents)):
                        raise MaintenanceBusy('工作区维护锁不能经过符号链接')
                    self.workspace.mkdir(parents=True, exist_ok=True)
                    assert self.lock_path is not None
                    self._fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
                    info = os.fstat(self._fd)
                    if not stat.S_ISREG(info.st_mode):
                        raise MaintenanceBusy('工作区维护锁不是普通文件')
                    mode = fcntl.LOCK_EX if self.exclusive else fcntl.LOCK_SH
                    fcntl.flock(self._fd, mode | fcntl.LOCK_NB)
                    self._file_identity = (info.st_dev, info.st_ino)
                self.check()
                return self
            except (MaintenanceBusy, MaintenanceLost):
                self.close()
                raise
            except Exception:
                self.close()
                raise MaintenanceBusy('无法取得维护保护；请检查数据库连接、工作区或仍在运行的写入进程') from None

    def check(self) -> None:
        """检查原PG后端和锁模式，不执行重新加锁；任何异常永久使本租约失效。"""
        with self._mutex:
            if self._lost or self.connection is None or self.connection.invalidated:
                self._lost = True
                raise MaintenanceLost('维护保护已经失效，当前操作停止；不能使用新连接接续旧备份')
            try:
                owned = self.connection.scalar(text(
                    "SELECT pg_backend_pid() = :pid AND EXISTS ("
                    "SELECT 1 FROM pg_locks WHERE locktype = 'advisory' "
                    "AND pid = pg_backend_pid() AND granted AND objsubid = 1 AND mode = :mode "
                    "AND classid::bigint = ((CAST(:key AS bigint) >> 32) & 4294967295) "
                    "AND objid::bigint = (CAST(:key AS bigint) & 4294967295))"
                ), {'pid': self.backend_pid, 'key': self.key,
                    'mode': 'ExclusiveLock' if self.exclusive else 'ShareLock'})
                if not owned:
                    raise MaintenanceLost('数据库维护锁已丢失')
                if self.lock_path is not None:
                    info = self.lock_path.lstat()
                    if not stat.S_ISREG(info.st_mode) or (info.st_dev, info.st_ino) != self._file_identity:
                        raise MaintenanceLost('工作区维护锁的文件身份已改变')
            except Exception:
                self._lost = True
                raise MaintenanceLost('维护保护已经失效，当前操作停止；不能使用新连接接续旧备份') from None

    def close(self) -> None:
        """断开专用连接释放会话锁；不把持锁连接送回池，不删除文件锁载体。"""
        with self._mutex:
            connection, self.connection = self.connection, None
            try:
                if connection is not None:
                    try:
                        connection.invalidate()
                    finally:
                        connection.close()
            finally:
                if self._fd is not None:
                    os.close(self._fd)
                    self._fd = None
                self._lost = True


@contextmanager
def maintenance_lease(engine: Engine, *, workspace: Path | None = None, exclusive=False) -> Iterator[MaintenanceLease]:
    lease = MaintenanceLease(engine, workspace=workspace, exclusive=exclusive).acquire()
    try:
        yield lease
        lease.check()
    finally:
        lease.close()


def managed_main[T](main: Callable[[], T]) -> T:
    """独立维护命令与运行服务遵循同一使用锁；只包入口，不改变业务事务。"""
    import sys

    from sqlalchemy import create_engine

    from knowledgeops_agent.core.config import get_settings

    if '--help' in sys.argv or '-h' in sys.argv:
        return main()
    settings = get_settings()
    engine = create_engine(settings.database_url, connect_args={'connect_timeout': 5})
    try:
        with maintenance_lease(engine, workspace=settings.workspace_root):
            return main()
    finally:
        engine.dispose()


def command_main() -> None:
    """为初始化整个命令组持锁；失锁时终止自己启动的子进程组，不碰其他应用。"""
    import argparse
    import signal
    import subprocess

    from sqlalchemy import create_engine

    from knowledgeops_agent.core.config import get_settings

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ['--'] else args.command
    if not command:
        parser.error('需要显式命令及参数')
    settings = get_settings()
    engine = create_engine(settings.database_url, connect_args={'connect_timeout': 5})
    try:
        with maintenance_lease(engine, workspace=settings.workspace_root) as lease:
            process = subprocess.Popen(command, start_new_session=True)
            try:
                while True:
                    lease.check()
                    try:
                        code = process.wait(timeout=1)
                        break
                    except subprocess.TimeoutExpired:
                        pass
                if code:
                    raise SystemExit(code)
            finally:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGTERM)
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait(timeout=5)
    finally:
        engine.dispose()


if __name__ == '__main__':
    command_main()
