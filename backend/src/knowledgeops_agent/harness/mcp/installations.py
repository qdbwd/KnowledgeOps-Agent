"""显式准备MCP依赖并生成固定入口；不在连接或工具调用时安装。

npm用package-lock+ci，uv用独立venv和带哈希的requirements锁。安装目录
从创建开始保持位置不变，避免移动venv后破坏绝对shebang。只有complete
清单写入后才能连接；文件锁覆盖准备、连接全生命周期与回收。
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import selectors
import shutil
import signal
import stat
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from uuid import UUID, uuid4

from knowledgeops_agent.core.resources import managed_resource_root, resource_path

FORMAT = 'knowledgeops-mcp-installation-v1'
MAX_METADATA = 4 * 1024 * 1024


class PreparationError(RuntimeError):
    """只携带稳定代码，不能把包管理器原始输出中的凭据写入任务或日志。"""


def _identity(value: str) -> str:
    if str(UUID(value)) != value:
        raise ValueError('MCP安装身份无效')
    return value


def _read(path: Path) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, 'rb') as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError('MCP安装清单必须是普通文件')
        data = stream.read(MAX_METADATA + 1)
    if len(data) > MAX_METADATA:
        raise ValueError('MCP安装清单超过限制')
    return data


def _sync(directory: Path):
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _json(path: Path, content: dict):
    temporary = path.with_name('.' + uuid4().hex + '.tmp')
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            json.dump(content, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _sync(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


class MCPInstallStorage:
    """只操作本应用登记的安装。外部command/cwd不因为被引用就归应用所有。"""

    def __init__(self, root: Path | None = None):
        self.root = (root or resource_path('MCP_DIRECTORY', managed_resource_root('mcp'))).absolute()

    def reject_external_references(self, config: dict):
        """受管位置只能由安装身份使用，不能通过外部入口别名绕过租约和依赖回收。

        检查声明的command/cwd/参数路径及已存在链接；不解析任意服务内部的
        配置语言，也不声称这是操作系统隔离。普通项目自备程序仍可放在其他目录。
        """
        if config.get('transport', 'stdio') != 'stdio':
            return
        protected = [self.root / name for name in ('releases', 'cache', 'data', 'ownership', '.env')]
        cwd = Path(config.get('cwd') or Path.cwd())
        command = config.get('command')
        executable = (shutil.which(command) or command) if command else None
        values = [str(cwd), executable, *config.get('args', [])]
        for value in values:
            if not value:
                continue
            # --file=/absolute/path 和单独的路径参数遵循同一归属规则。
            value = value.split('=', 1)[1] if value.startswith('-') and '=' in value else value
            if value.startswith('-'):
                continue
            path = Path(value)
            path = Path(os.path.abspath(path if path.is_absolute() else cwd / path))
            try:
                candidates = (path, path.resolve())
            except (OSError, RuntimeError):
                raise ValueError('MCP路径无法核对；不能借用受管目录') from None
            if any(candidate.is_relative_to(root) for candidate in candidates for root in protected):
                raise ValueError('MCP受管目录不能作为外部入口或参数借用，请通过原连接或重新准备独立安装')

    def checked(self, *parts: str) -> Path:
        if any(path.is_symlink() for path in (self.root, *self.root.parents)):
            raise ValueError('MCP目录不能经过链接')
        current = self.root
        for part in parts:
            if not part or part in {'.', '..'} or '/' in part or '\\' in part:
                raise ValueError('MCP管理路径无效')
            current /= part
            if current.is_symlink():
                raise ValueError('MCP管理目录不能经过链接')
        return current

    def release(self, installation: str) -> Path:
        return self.checked('releases', _identity(installation))

    @contextmanager
    def _ownership_lock(self):
        """目录归属/删除日志位于被删目录之外，部分rmtree失败后仍能核对和重试。"""
        self.checked().mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(self.checked('.ownership.lock'), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise ValueError('MCP归属锁必须为普通文件')
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            os.close(descriptor)

    def _journal_path(self, area, identity):
        if area not in {'releases', 'cache', 'data'}:
            raise ValueError('MCP目录类型无效')
        return self.checked('ownership', area, _identity(identity) + '.json')

    def _journal(self, area, identity):
        path = self._journal_path(area, identity)
        return json.loads(_read(path)) if path.exists() else None

    def _write_journal(self, area, identity, marker, phase):
        path = self._journal_path(area, identity)
        path.parent.mkdir(parents=True, exist_ok=True)
        _json(path, {'owner': marker, 'phase': phase})

    def _create_owned(self, area, identity, marker):
        """先登记保留路径，再创建目录；创建中断也有精确归属，不扫描猜测未知文件。"""
        directory = self.checked(area, _identity(identity))
        self.checked(area).mkdir(parents=True, exist_ok=True)
        journal = self._journal(area, identity)
        if journal and (journal.get('owner') != marker or journal.get('phase') == 'removing'):
            raise ValueError('MCP服务目录归属冲突或正在回收')
        if journal and journal.get('phase') == 'removed':
            if directory.exists():
                raise ValueError('MCP已回收路径出现未登记文件')
            self._write_journal(area, identity, marker, 'reserved')
        if directory.exists() and not journal:
            try:
                if json.loads(_read(directory / 'owner.json')) != marker:
                    raise ValueError
            except (OSError, ValueError):
                raise ValueError('MCP服务目录缺少有效归属记录') from None
        if journal is None:
            self._write_journal(area, identity, marker, 'reserved')
        directory.mkdir(mode=0o700, exist_ok=True)
        if (directory / 'owner.json').exists():
            if json.loads(_read(directory / 'owner.json')) != marker:
                raise ValueError('MCP服务目录归属冲突')
        else:
            _json(directory / 'owner.json', marker)
        return directory

    def service_directory(self, area: str, connection: str) -> Path:
        """缓存/服务数据与安装目录分开，只有带归属的目录才可由本应用管理。"""
        if area not in {'cache', 'data'}:
            raise ValueError('MCP服务目录类型无效')
        directory = self.checked(area, _identity(connection))
        marker = {'format': FORMAT, 'connection_id': connection, 'area': area}
        with self._ownership_lock():
            self._create_owned(area, connection, marker)
            self._write_journal(area, connection, marker, 'active')
        return directory

    def create(self, connection: str, installation: str, operation: str) -> Path:
        directory = self.release(installation)
        marker = {'format': FORMAT, 'connection_id': _identity(connection),
                  'installation_id': installation, 'operation_id': _identity(operation)}
        with self._ownership_lock():
            # 准备重试使用新身份，不能重新执行已有安装的构建脚本。
            if directory.exists() or self._journal('releases', installation):
                raise ValueError('MCP安装身份已经使用')
            self._create_owned('releases', installation, marker)
            descriptor = os.open(directory / '.use.lock', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.close(descriptor)
            _sync(directory)
            _sync(directory.parent)
            self._write_journal('releases', installation, marker, 'active')
        return directory

    def owner(self, installation: str) -> dict:
        try:
            journal = self._journal('releases', installation)
            if journal and journal.get('phase') != 'active':
                raise ValueError
            marker = json.loads(_read(self.release(installation) / 'owner.json'))
            if marker['format'] != FORMAT or marker['installation_id'] != installation:
                raise ValueError
            _identity(marker['connection_id'])
            _identity(marker['operation_id'])
            if journal and journal['owner'] != marker:
                raise ValueError
        except (OSError, KeyError, ValueError):
            raise ValueError('MCP安装目录缺少有效归属记录') from None
        return marker

    @contextmanager
    def lock(self, installation: str, *, exclusive=False, wait=True):
        self.owner(installation)
        descriptor = os.open(self.release(installation) / '.use.lock', os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise ValueError('MCP使用锁必须是普通文件')
            fcntl.flock(descriptor, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
                        | (0 if wait else fcntl.LOCK_NB))
            self.owner(installation)  # 等待锁期间可能已经完成删除。
            yield
        finally:
            os.close(descriptor)

    def complete(self, installation: str, result: dict) -> dict:
        directory = self.release(installation)
        manifest = {**result, **self.owner(installation)}
        _json(directory / 'complete.json', manifest)
        return manifest

    def manifest(self, installation: str) -> dict:
        manifest, _ = self.portable(installation)
        directory = self.release(installation)
        entry = directory / manifest['entrypoint']
        if not entry.resolve().is_relative_to(directory.resolve()) or not entry.is_file() or not os.access(entry, os.X_OK):
            raise ValueError('MCP安装入口缺失或越界，请重新准备候选版本')
        return manifest

    def portable(self, installation: str) -> tuple[dict, dict[str, str]]:
        """只读取完成清单与经过摘要核对的锁文件；恢复占位不要求有可执行环境。"""
        directory = self.release(installation)
        owner = self.owner(installation)
        try:
            manifest = json.loads(_read(directory / 'complete.json'))
        except FileNotFoundError:
            raise ValueError('MCP依赖尚未准备完成') from None
        if any(manifest.get(key) != value for key, value in owner.items()):
            raise ValueError('MCP安装身份不匹配')
        locks = {}
        for name, digest in manifest['lock_files'].items():
            if name not in {'package.json', 'package-lock.json', 'requirements.in', 'requirements.lock'}:
                raise ValueError('MCP锁文件名称无效')
            data = _read(directory / name)
            if hashlib.sha256(data).hexdigest() != digest:
                raise ValueError('MCP锁文件已改变，请重新准备候选版本')
            locks[name] = data.decode('utf-8')
        return manifest, locks

    @contextmanager
    def use(self, installation: str):
        # 同步短文件锁在异步连接创建前获取；清理用非阻塞独占锁，不等待调用结束。
        with self.lock(installation):
            yield self.manifest(installation)

    def remove(self, installation: str, *, connection: str | None = None, operation: str | None = None) -> bool:
        directory = self.release(installation)
        with self._ownership_lock():
            journal = self._journal('releases', installation)
            if not directory.exists() and journal is None:
                # DB已经分配过的安装即使尚未建目录，也保留墓碑，阻止延迟旧worker重建。
                if connection and operation:
                    self._write_journal('releases', installation, {'format': FORMAT,
                        'installation_id': installation, 'connection_id': _identity(connection),
                        'operation_id': _identity(operation)}, 'removed')
                return True
            marker = journal['owner'] if journal else self.owner(installation)
            if (marker.get('format') != FORMAT or marker.get('installation_id') != installation
                or (connection and marker.get('connection_id') != connection)
                or (operation and marker.get('operation_id') != operation)):
                raise ValueError('MCP安装归属与数据库不符')
            if journal and journal.get('phase') == 'removed':
                if directory.exists():
                    raise ValueError('MCP已回收路径出现未登记文件')
                return True
            if journal and journal.get('phase') in {'reserved', 'removing'}:
                self._remove_owned('releases', installation, marker)
            else:
                try:
                    with self.lock(installation, exclusive=True, wait=False):
                        self._remove_owned('releases', installation, marker)
                except BlockingIOError:
                    return False
        return True

    def _remove_owned(self, area, identity, marker):
        directory = self.checked(area, _identity(identity))
        self._write_journal(area, identity, marker, 'removing')
        if directory.exists():
            shutil.rmtree(directory)  # 根已检查；rmtree不跟随内部符号链接。
            _sync(directory.parent)
        self._write_journal(area, identity, marker, 'removed')

    def remove_service(self, area: str, connection: str) -> None:
        """调用方先证明该服务已无运行/安装依赖；外部工作目录从不进入本入口。"""
        if area not in {'cache', 'data'}:
            raise ValueError('MCP服务目录类型无效')
        directory = self.checked(area, _identity(connection))
        marker = {'format': FORMAT, 'connection_id': connection, 'area': area}
        with self._ownership_lock():
            journal = self._journal(area, connection)
            if not directory.exists() and journal is None:
                return
            actual = journal.get('owner') if journal else json.loads(_read(directory / 'owner.json'))
            if actual != marker:
                raise ValueError('MCP服务目录缺少有效归属记录')
            if journal and journal.get('phase') == 'removed':
                if directory.exists():
                    raise ValueError('MCP已回收路径出现未登记文件')
                return
            if journal is None or journal.get('phase') == 'active':
                if json.loads(_read(directory / 'owner.json')) != marker:
                    raise ValueError('MCP服务目录归属已改变')
            self._remove_owned(area, connection, marker)

    def forget(self, installation: str) -> None:
        """仅供数据库已证明无历史依赖且超过保留期的安装释放小型墓碑。"""
        with self._ownership_lock():
            if self.release(installation).exists():
                raise ValueError('MCP安装目录仍存在，不能释放归属')
            journal = self._journal('releases', installation)
            if journal:
                if journal.get('phase') != 'removed':
                    raise ValueError('MCP安装尚未完成回收')
                self._journal_path('releases', installation).unlink()

    def forget_service(self, connection: str) -> None:
        """服务目录已经释放且超过清理日志保留期后，回收目录外的小型归属日志。"""
        with self._ownership_lock():
            for area in ('cache', 'data'):
                if self.checked(area, _identity(connection)).exists():
                    raise ValueError('MCP服务目录仍存在，不能释放归属')
                journal = self._journal(area, connection)
                if journal:
                    if journal.get('phase') != 'removed':
                        raise ValueError('MCP服务目录尚未完成回收')
                    self._journal_path(area, connection).unlink()


def _stop(process):
    """即使安装主进程已退出，也终止其同组遗留子进程，不让安装脚本驻留。"""
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait(timeout=5)


def run_preparation_command(argv, *, cwd, env, phase, check, report, deadline):
    """有界等待、持续检查租约/取消、仅保存结构化状态，不落盘原始安装输出。"""
    check()
    report(phase, 'running')
    process = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
    tail = bytearray()
    try:
        assert process.stdout is not None
        os.set_blocking(process.stdout.fileno(), False)
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while process.poll() is None:
                check()
                if time.monotonic() >= deadline:
                    raise PreparationError('preparation_timeout')
                for key, _ in selector.select(timeout=0.2):
                    data = os.read(key.fd, 8192)
                    tail.extend(data)
                    del tail[:-8192]
                    if not data:
                        selector.unregister(key.fileobj)
            check()
        if process.returncode:
            # 只提取确定错误分类；第三方日志可能携带URL或构建环境，不原样回送。
            code = 'package_not_found' if any(word in tail for word in (b'E404', b'No solution found')) else 'package_command_failed'
            report(phase, code)
            raise PreparationError(code)
        report(phase, 'succeeded')
    finally:
        _stop(process)
        if process.stdout is not None:
            process.stdout.close()


class MCPPreparer:
    def __init__(self, storage: MCPInstallStorage, *, runner=run_preparation_command):
        self.storage, self.runner = storage, runner

    def prepare(self, connection: str, installation: str, operation: str, request: dict, *,
                check=lambda: None, report=lambda *args: None, timeout=600, lock_files=None) -> dict:
        """仅供明确的准备操作调用；lock_files用于迁移后的显式按锁重建。"""
        from knowledgeops_agent.harness.mcp.imports import _entry, _package

        manager, packages = request.get('manager'), request.get('packages')
        if manager not in {'npm', 'uv'} or not isinstance(packages, list) or not 1 <= len(packages) <= 17:
            raise ValueError('MCP依赖准备请求无效')
        packages = [_package(item, manager) for item in packages]
        entry = _entry(request['entrypoint']) if request.get('entrypoint') else None
        if manager == 'uv' and not entry:
            raise ValueError('Python安装需要明确的入口名称')
        check()
        directory = self.storage.create(connection, installation, operation)
        with self.storage.lock(installation, exclusive=True):
            cache = self.storage.service_directory('cache', connection)
            # HOME/配置/临时目录也归本次安装，包管理器不自动加载应用的部署配置。
            for name in ('home', 'tmp'):
                (directory / name).mkdir()
            env = {key: value for key, value in os.environ.items() if key in {'PATH', 'LANG', 'LC_ALL'}}
            env.update(HOME=str(directory / 'home'), TMPDIR=str(directory / 'tmp'),
                       UV_CACHE_DIR=str(cache / 'uv'), UV_PYTHON_DOWNLOADS='never', UV_NO_CONFIG='1',
                       NPM_CONFIG_CACHE=str(cache / 'npm'), NPM_CONFIG_UPDATE_NOTIFIER='false',
                       NPM_CONFIG_LOGS_MAX='0', NPM_CONFIG_AUDIT='false', NPM_CONFIG_FUND='false')
            for kind in ('user', 'global'):
                path = directory / (kind + '.npmrc')
                path.write_text('', encoding='utf-8')
                env['NPM_CONFIG_' + kind.upper() + 'CONFIG'] = str(path)
            deadline = time.monotonic() + timeout

            def command(argv, phase):
                self.runner(argv, cwd=directory, env=env, phase=phase, check=check, report=report, deadline=deadline)

            allowed = {'package.json', 'package-lock.json'} if manager == 'npm' else {'requirements.in', 'requirements.lock'}
            if lock_files is not None:
                if set(lock_files) != allowed or any(not isinstance(value, str) or len(value.encode()) > MAX_METADATA for value in lock_files.values()):
                    raise ValueError('MCP恢复锁文件不完整或超过限制')
                for name, value in lock_files.items():
                    (directory / name).write_text(value, encoding='utf-8')
            if manager == 'npm':
                npm = shutil.which('npm')
                if not npm:
                    raise PreparationError('npm_not_installed')
                if lock_files is None:
                    _json(directory / 'package.json', {'private': True, 'name': 'knowledgeops-mcp-' + installation,
                                                      'version': '1.0.0'})
                    command([npm, 'install', '--package-lock-only', '--save-exact', '--ignore-scripts', '--', *packages], 'resolve')
                command([npm, 'ci', '--no-audit', '--no-fund'], 'install')
                if entry is None:
                    name = packages[0].split('@', 2)[1] if packages[0].startswith('@') else packages[0].split('@', 1)[0]
                    if packages[0].startswith('@'):
                        name = '@' + name
                    package = json.loads(_read(directory / 'node_modules' / name / 'package.json'))
                    bins = package.get('bin', {})
                    if isinstance(bins, str):
                        bins = {name.rsplit('/', 1)[-1]: bins}
                    # 采用npx的单入口/同目标/与包名同名规则，歧义要求用户显式--package入口。
                    if len(set(bins.values())) == 1:
                        entry = next(iter(bins))
                    elif name.rsplit('/', 1)[-1] in bins:
                        entry = name.rsplit('/', 1)[-1]
                    else:
                        raise PreparationError('ambiguous_entrypoint')
                relative = 'node_modules/.bin/' + _entry(entry)
            else:
                uv = shutil.which('uv')
                if not uv:
                    raise PreparationError('uv_not_installed')
                python = request.get('python') or sys.executable
                command([uv, 'venv', '--no-python-downloads', '--python', python, '.venv'], 'environment')
                python_path = str(directory / '.venv/bin/python')
                if lock_files is None:
                    (directory / 'requirements.in').write_text('\n'.join(packages) + '\n', encoding='utf-8')
                    command([uv, 'pip', 'compile', 'requirements.in', '--generate-hashes', '--output-file',
                             'requirements.lock', '--python', python_path, '--no-python-downloads'], 'resolve')
                command([uv, 'pip', 'sync', 'requirements.lock', '--require-hashes', '--python', python_path,
                         '--no-python-downloads'], 'install')
                assert entry is not None  # 请求校验已要求Python入口，保留类型收窄。
                relative = '.venv/bin/' + entry
            check()
            executable = directory / relative
            if not executable.resolve().is_relative_to(directory.resolve()) or not executable.is_file() or not os.access(executable, os.X_OK):
                raise PreparationError('entrypoint_not_installed')
            manifest = self.storage.complete(installation, {'request': request, 'entrypoint': relative,
                'lock_files': {name: hashlib.sha256(_read(directory / name)).hexdigest() for name in allowed}})
            report('prepared', 'succeeded')
            return {**manifest, 'command': str(executable)}
