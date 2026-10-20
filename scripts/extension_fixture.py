"""仅在隔离备份库中准备两个真实MCP包，并核对跨路径按锁重建与协议调用。"""

import argparse
import asyncio
import hashlib
import json
import os
import re
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from knowledgeops_agent.core.config import Settings
from knowledgeops_agent.harness.mcp.management import MCPManagement
from knowledgeops_agent.harness.mcp.registry import MCPRegistry
from knowledgeops_agent.harness.mcp.secrets import config_references
from knowledgeops_agent.harness.mcp.transport import MCPConnectionConfig, connected_tools
from knowledgeops_agent.harness.skills.management import SkillManagement
from knowledgeops_agent.persistence import models as db

MARKER = 'extension-backup-fixture'


def seed(sessions, root):
    skills = SkillManagement(sessions, root=root / 'skills')
    # 真实管理接口保存完整包；配套脚本及二进制资源必须逐字节恢复。
    files = [('SKILL.md', b'---\nname: backup-script\ndescription: Read a local fixture.\n---\nRead scripts/probe.py.'),
             ('scripts/probe.py', b'from pathlib import Path\nprint(Path(__file__).parents[1].joinpath("assets/data.txt").read_text())\n'),
             ('assets/data.txt', b'portable-extension-fixture'), ('assets/binary.bin', bytes(range(256)))]
    # 管理服务的预览/确认协议由调用点核对，不直接伪造技能状态。
    preview = skills.stage(files)
    skills.commit(preview['id'])
    from knowledgeops_agent.harness.runtime.resources import content_hash
    from knowledgeops_agent.harness.tools.policy import ToolPolicyService

    policy = ToolPolicyService(sessions).get()
    # 备份前新增技能会生成一次工具策略修订；冻结旧任务仍保持原值。
    with sessions.begin() as session:
        for run in session.scalars(select(db.Run)):
            if expected := run.input_data.get('backup_increment'):
                run.input_data = {**run.input_data, 'backup_increment': {**expected,
                    'current_policy_hash': content_hash(policy), 'current_policy_revision': policy['revision']}}
    manager = MCPManagement(sessions, root=root / 'mcp')
    saved = []
    for runner, package, tool in [('npx', '@modelcontextprotocol/server-memory', 'read_graph'),
                                  ('uvx', 'mcp-server-time', 'get_current_time')]:
        candidate = manager.import_config({'name': 'backup-' + runner, 'command': runner, 'args': [package],
                                           'env': {'BACKUP_FIXTURE_TOKEN': 'fixture-secret-never-archive'}})[0]
        queued = manager.prepare(candidate['id'])
        ready = manager.execute(candidate['id'], queued['attempts'])
        if ready is None:
            raise RuntimeError('fixture preparation was not claimed')
        if ready['status'] != 'prepared':
            raise RuntimeError('fixture package preparation failed: ' + str(ready['error_code']))
        tested = manager.test(candidate['id'])
        if tested['status'] != 'tested':
            raise RuntimeError('fixture protocol discovery failed: ' + str(tested['error_code']))
        connection = manager.activate(candidate['id'], enabled=True,
            permissions={item['name']: 'read' if item['name'] == tool else 'deny' for item in tested['result']['tools']})
        installation = connection['config']['installation_id']
        info, _ = manager.storage.portable(installation)
        (manager.storage.service_directory('data', connection['id']) / 'fixture.json').write_text('{"preserved":true}')
        saved.append({'connection': connection['id'], 'installation': installation, 'tool': tool,
                      'locks': info['lock_files'], 'old_revision': connection['revision']})
    with sessions.begin() as session:
        session.add(db.MaintenanceState(name=MARKER, cursor={'connections': saved,
            'skills': {name: hashlib.sha256(data).hexdigest() for name, data in files}}))
    return {'prepared_and_discovered': len(saved), 'model_calls': 0}


def rebuild(sessions, root):
    manager = MCPManagement(sessions, root=root / 'mcp')
    with sessions() as session:
        marker = session.get(db.MaintenanceState, MARKER)
        if not marker:
            raise ValueError('只验证本脚本创建的隔离扩展种子')
        expected = marker.cursor
    if manager.secrets.project_values():
        raise ValueError('恢复不应自动复制项目密钥')
    for name, digest in expected['skills'].items():
        if hashlib.sha256((root / 'skills' / 'backup-script' / name).read_bytes()).hexdigest() != digest:
            raise ValueError('完整技能字节没有恢复')
    reports = []
    for item in expected['connections']:
        connection = MCPRegistry(sessions).get(item['connection'])
        if connection['enabled'] or connection['revision'] <= item['old_revision']:
            raise ValueError('恢复后不能沿用旧授权')
        if not (manager.storage.checked('data', item['connection']) / 'fixture.json').is_file():
            raise ValueError('显式备份的服务数据没有恢复')
        candidate = next(row for row in manager.list() if row['parameters'].get('restore_from') == item['installation'])
        statuses = manager.credentials(candidate['id'])
        manager.credentials(candidate['id'], values={row['reference']: 'fixture-restored-value' for row in statuses})
        queued = manager.prepare(candidate['id'])
        ready = manager.execute(candidate['id'], queued['attempts'])
        if ready is None:
            raise RuntimeError('restore preparation was not claimed')
        if ready['status'] != 'prepared':
            raise RuntimeError('restore preparation failed: ' + str(ready['error_code']))
        info, _ = manager.storage.portable(ready['result']['installation_id'])
        if info['lock_files'] != item['locks'] or any(row['phase'] == 'resolve' for row in ready['result']['progress']):
            raise ValueError('恢复没有沿用原锁文件')
        tested = manager.test(candidate['id'])
        if tested['status'] != 'tested':
            raise RuntimeError('restored protocol discovery failed')
        current = manager.activate(candidate['id'], enabled=True,
            permissions={tool['name']: 'read' if tool['name'] == item['tool'] else 'deny' for tool in tested['result']['tools']})

        async def invoke(config, name):
            async with connected_tools(MCPConnectionConfig.model_validate(config),
                                       secrets=manager.secrets.resolve(config_references(config))) as tools:
                tool = next(tool for tool in tools if tool.name == name)
                arguments = {'timezone': 'UTC'} if name == 'get_current_time' else {}
                result = await tool.ainvoke({'name': name, 'args': arguments, 'id': 'restored-protocol', 'type': 'tool_call'})
                if result.status != 'success':
                    raise RuntimeError('restored protocol call failed')

        asyncio.run(invoke(current['config'], item['tool']))
        reports.append({'connection': item['connection'], 'locks_identical': True, 'protocol_call': True})
    return {'skill_bytes_identical': True, 'project_secrets_excluded': True, 'connections': reports, 'model_calls': 0}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=['seed', 'rebuild'])
    parser.add_argument('--database', required=True)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    prefix = 'backupfixture' if args.mode == 'seed' else 'restore'
    if not re.fullmatch('knowledgeops_' + prefix + r'_[a-f0-9]{12}', args.database):
        parser.error('仅允许本轮创建的隔离数据库')
    if not args.root.resolve().is_relative_to(Path('/workspace/artifacts/verification')):
        parser.error('扩展演练只能写入本项目artifacts/verification')
    os.environ['MCP_DIRECTORY'] = str(args.root / 'mcp')
    engine = create_engine(make_url(Settings(model_calls_enabled=False).database_url).set(database=args.database))
    try:
        result = (seed if args.mode == 'seed' else rebuild)(sessionmaker(engine, expire_on_commit=False), args.root)
        if args.output:
            args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2))
        print(json.dumps(result))
    finally:
        engine.dispose()


if __name__ == '__main__':
    main()
