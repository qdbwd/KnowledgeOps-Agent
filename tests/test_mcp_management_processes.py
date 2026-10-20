"""两个真正Python进程共享PG/项目密钥文件，实际stdio子进程随轮换与撤权退出。"""

import json
import os
import selectors
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

from sqlalchemy import text

from knowledgeops_agent.harness.mcp.installations import MCPInstallStorage
from knowledgeops_agent.harness.mcp.registry import MCPRegistry
from knowledgeops_agent.harness.mcp.secrets import MCPSecretStore
from knowledgeops_agent.persistence.models import ExtensionOperation, MCPInstallation


def read_message(process):
    with selectors.DefaultSelector() as selector:
        selector.register(process.stdout, selectors.EVENT_READ)
        assert selector.select(timeout=45), 'MCP worker did not respond'
        line = process.stdout.readline()
    assert line, 'MCP worker exited'
    return json.loads(line)


def request(process):
    process.stdin.write('counter\n')
    process.stdin.flush()
    return read_message(process)


def exited(pids):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if all(not Path(f'/proc/{pid}').exists() for pid in pids):
            return
        time.sleep(0.1)
    raise AssertionError('old MCP process survived rotation or revocation')


def test_two_real_worker_pools_invalidate_sessions_and_hold_installation_files(business_sessions, tmp_path, monkeypatch):
    monkeypatch.setenv('MCP_DIRECTORY', str(tmp_path))
    storage = MCPInstallStorage(tmp_path)
    installation, operation = (str(uuid4()) for _ in range(2))
    entry = storage.release(installation) / 'server'
    secrets = MCPSecretStore(tmp_path)
    secrets.update({'ROTATION_TOKEN': 'first-test-token'})
    registry = MCPRegistry(business_sessions)
    row = registry.create('process-pool', {'transport': 'stdio', 'command': str(entry), 'protocol_mode': 'legacy',
        'installation_id': installation, 'environment_refs': {'TOKEN': 'ROTATION_TOKEN'}})
    storage.create(row['id'], installation, operation)
    entry.write_text('#!' + sys.executable + '\n' + Path('tests/fixtures/mcp_stateful.py').read_text())
    entry.chmod(0o755)
    manifest = storage.complete(installation, {'entrypoint': 'server', 'request': {}, 'lock_files': {}})
    # 已准备的本地安装同时具有磁盘归属和PG登记；缺失登记的恢复安装不能启用。
    with business_sessions.begin() as session:
        session.add(ExtensionOperation(id=operation, kind='mcp_candidate', target=row['id'], status='prepared'))
        session.flush()
        session.add(MCPInstallation(id=installation, connection_id=row['id'], operation_id=operation,
                                    status='prepared', request={}, manifest=manifest))
    row = registry.update(row['id'], expected_revision=row['revision'], enabled=True)
    engine = business_sessions.kw['bind']
    with engine.connect() as sql:
        schema = sql.scalar(text('SELECT current_schema()'))
    env = {key: value for key, value in os.environ.items() if key in {'PATH', 'LANG', 'LC_ALL'}}
    env.update(MCP_DIRECTORY=str(tmp_path), MCP_TEST_DATABASE_URL=engine.url.render_as_string(hide_password=False),
               MCP_TEST_SCHEMA=schema, MCP_TEST_CONNECTION=row['id'])
    workers, logs = [], []
    try:
        for index in range(2):
            log = (tmp_path / f'worker-{index}.log').open('w')
            logs.append(log)
            workers.append(subprocess.Popen([sys.executable, 'tests/fixtures/mcp_pool_worker.py'],
                env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log, text=True, bufsize=1))
        assert all(read_message(worker) == {'ready': True} for worker in workers)
        first = [request(worker) for worker in workers]
        again = [request(worker) for worker in workers]
        assert first[0]['result']['pid'] != first[1]['result']['pid']
        assert all(item['result']['count'] == 2 and item['reused'] for item in again)
        assert storage.remove(installation) is False
        secrets.update({'ROTATION_TOKEN': 'second-test-token'})
        exited([item['result']['pid'] for item in first])
        rotated = [request(worker) for worker in workers]
        assert all(item['result']['count'] == 1 and not item['reused'] for item in rotated)
        registry.update(row['id'], expected_revision=row['revision'], enabled=False)
        exited([item['result']['pid'] for item in rotated])
        assert all('error' in request(worker) for worker in workers)
        assert storage.remove(installation) is True
    finally:
        for worker in workers:
            if worker.poll() is None:
                worker.stdin.write('close\n')
                worker.stdin.flush()
                try:
                    worker.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    worker.kill()
                    worker.wait(timeout=5)
            worker.stdin.close()
            worker.stdout.close()
        for log in logs:
            log.close()
    for log in tmp_path.glob('worker-*.log'):
        assert 'first-test-token' not in log.read_text() and 'second-test-token' not in log.read_text()
