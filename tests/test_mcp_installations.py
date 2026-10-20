"""固定MCP安装的路径、使用锁、显式准备与进程取消合同。"""

import json
import os
import sys
import time
from pathlib import Path
from uuid import uuid4

import pytest

from knowledgeops_agent.harness.mcp.installations import (
    MCPInstallStorage,
    MCPPreparer,
    PreparationError,
    run_preparation_command,
)


def identities():
    return str(uuid4()), str(uuid4()), str(uuid4())


def test_only_owned_releases_can_be_removed_and_a_live_reader_blocks_removal(tmp_path):
    storage = MCPInstallStorage(tmp_path)
    connection, installation, operation = identities()
    directory = storage.create(connection, installation, operation)
    (directory / 'server').write_text('ready')
    (directory / 'server').chmod(0o755)
    storage.complete(installation, {'entrypoint': 'server', 'request': {}, 'lock_files': {}})
    with storage.use(installation) as manifest:
        assert manifest['connection_id'] == connection
        assert storage.remove(installation) is False
        assert directory.exists()
    assert storage.remove(installation) is True
    assert not directory.exists()
    unknown = str(uuid4())
    (tmp_path / 'releases' / unknown).mkdir()
    with pytest.raises(ValueError, match='归属'):
        storage.remove(unknown)
    assert (tmp_path / 'releases' / unknown).exists()


def test_preparation_failure_never_creates_a_runnable_manifest(tmp_path):
    storage = MCPInstallStorage(tmp_path)
    connection, installation, operation = identities()
    calls = []

    def fail(argv, **options):
        calls.append(argv)
        raise PreparationError('package_not_found')

    preparer = MCPPreparer(storage, runner=fail)
    with pytest.raises(PreparationError):
        preparer.prepare(connection, installation, operation,
                         {'manager': 'npm', 'packages': ['missing@1'], 'entrypoint': None, 'python': None})
    assert len(calls) == 1
    with pytest.raises(ValueError, match='准备'):
        with storage.use(installation):
            pytest.fail('failed installations must not run')
    assert storage.remove(installation)


def test_npm_resolves_then_installs_from_lock_and_uses_declared_entry(tmp_path, monkeypatch):
    monkeypatch.setenv('DATABASE_URL', 'must-not-inherit')
    monkeypatch.setenv('MODEL_API_KEY', 'must-not-inherit')
    storage = MCPInstallStorage(tmp_path)
    connection, installation, operation = identities()
    calls = []

    def npm(argv, *, cwd, env, **options):
        calls.append(argv)
        assert 'DATABASE_URL' not in env and 'MODEL_API_KEY' not in env
        if 'install' in argv:
            (cwd / 'package-lock.json').write_text(json.dumps({'lockfileVersion': 3}))
        if 'ci' in argv:
            package = cwd / 'node_modules' / '@example' / 'server'
            package.mkdir(parents=True)
            (package / 'package.json').write_text(json.dumps({'name': '@example/server', 'bin': {'serve': 'cli.js'}}))
            (package / 'cli.js').write_text('#!/usr/bin/env node\n')
            (package / 'cli.js').chmod(0o755)
            bins = cwd / 'node_modules' / '.bin'
            bins.mkdir()
            (bins / 'serve').symlink_to('../@example/server/cli.js')

    result = MCPPreparer(storage, runner=npm).prepare(connection, installation, operation,
        {'manager': 'npm', 'packages': ['@example/server@1'], 'entrypoint': None, 'python': None})
    assert 'install' in calls[0] and '--package-lock-only' in calls[0]
    assert 'ci' in calls[1]
    assert result['entrypoint'] == 'node_modules/.bin/serve'
    assert result['lock_files']['package-lock.json']
    assert Path(result['command']).is_file()
    with storage.use(installation):
        pass
    (storage.release(installation) / 'package-lock.json').write_text('{}')
    with pytest.raises(ValueError, match='锁文件'):
        with storage.use(installation):
            pytest.fail('modified lock must not be trusted')


def test_managed_paths_never_follow_external_links(tmp_path):
    outside = tmp_path / 'outside'
    outside.mkdir()
    root = tmp_path / 'mcp'
    root.mkdir()
    (root / 'releases').symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match='链接'):
        MCPInstallStorage(root).create(*identities())
    assert not list(outside.iterdir())


def test_cancellation_kills_the_real_process_group_without_persisting_output(tmp_path):
    marker = tmp_path / 'child.pid'
    code = ('import subprocess,time,pathlib; '
            'p=subprocess.Popen(["sleep","60"]); '
            f'pathlib.Path({str(marker)!r}).write_text(str(p.pid)); '
            'print("secret-output",flush=True); time.sleep(60)')

    def check():
        if marker.exists():
            raise PreparationError('cancelled')

    with pytest.raises(PreparationError, match='cancelled') as error:
        run_preparation_command([sys.executable, '-c', code], cwd=tmp_path,
            env={'PATH': os.environ['PATH']}, phase='test', check=check, report=lambda *args: None,
            deadline=time.monotonic() + 10)
    assert 'secret-output' not in str(error.value)
    pid = marker.read_text()
    state = Path('/proc') / pid / 'stat'
    assert not state.exists() or state.read_text().split()[2] == 'Z'


@pytest.mark.skipif(os.environ.get('VERIFY_MCP_PACKAGE_NETWORK') != '1', reason='explicit package network verification')
@pytest.mark.parametrize('manager,package,entry', [
    ('npm', '@modelcontextprotocol/server-memory', None),
    ('uv', 'mcp-server-time', 'mcp-server-time'),
])
def test_real_package_preparation_and_protocol_discovery(tmp_path, manager, package, entry):
    import asyncio

    from knowledgeops_agent.harness.mcp.transport import MCPConnectionConfig, connected_tools

    storage = MCPInstallStorage(tmp_path)
    connection, installation, operation = identities()
    result = MCPPreparer(storage).prepare(connection, installation, operation,
        {'manager': manager, 'packages': [package], 'entrypoint': entry, 'python': None})
    assert result['lock_files']

    async def discover():
        async with connected_tools(MCPConnectionConfig(transport='stdio', command=result['command'],
                                                       protocol_mode='legacy'), secrets={}) as tools:
            assert tools
            # npm memory读取空图；uv time读取时间，均不修改业务数据。
            name, args = ('read_graph', {}) if manager == 'npm' else ('get_current_time', {'timezone': 'UTC'})
            tool = next(tool for tool in tools if tool.name == name)
            answer = await tool.ainvoke({'name': name, 'args': args, 'id': 'install-check', 'type': 'tool_call'})
            assert answer.status == 'success'

    asyncio.run(discover())
