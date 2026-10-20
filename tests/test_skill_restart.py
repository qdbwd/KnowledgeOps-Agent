"""两个独立worker进程共用真实PG检查点，验证技能更新与撤回后的任务接续。"""

import json
import subprocess
import sys
from uuid import uuid4

import pytest
from sqlalchemy import delete, select
from test_api_worker import client as _client
from test_api_worker import services as _services

from knowledgeops_agent.harness.permissions.settings import ExecutionSettings
from knowledgeops_agent.harness.skills.management import SkillManagement
from knowledgeops_agent.harness.tools.catalog import tool_options
from knowledgeops_agent.harness.tools.policy import ToolPolicyService
from knowledgeops_agent.persistence.models import RunEvent, RunSkillUse

client = _client
services = _services


@pytest.mark.parametrize('revoked', [False, True])
def test_new_worker_resumes_original_package_or_rejects_revoked_permission(client, services, tmp_path, monkeypatch, revoked):
    directory = tmp_path / 'skills'
    monkeypatch.setenv('SKILLS_DIRECTORY', str(directory))
    services.settings.workspace_root = tmp_path / 'workspace'
    manager = SkillManagement(services.sessions, directory)
    initial = manager.stage([
        ('SKILL.md', b'---\nname: restore-skill\ndescription: generate a local file\n---\nRun scripts/run.py'),
        ('scripts/run.py', b"from pathlib import Path\np=Path(__file__).parent.parent/'assets/data.bin'\nPath('outputs/restored.txt').write_text(p.read_bytes().hex())\nprint('original-resource')"),
        ('assets/data.bin', b'\xff\x00\x81'),
    ])
    manager.commit(initial['id'])
    policy = ToolPolicyService(services.sessions)
    policy.update(expected_revision=policy.get()['revision'], skill_enabled={'restore-skill': True},
                  enabled={**{row['id']: True for row in tool_options() if row['condition'] == 'workspace'},
                           'builtin:ask_clarification': True})
    conversation = client.post('/api/conversations', json={'title': '技能进程恢复'}).json()
    ExecutionSettings(services.sessions).update(conversation['id'], expected_revision=0, permission_mode='auto_review')
    response = client.post(f"/api/conversations/{conversation['id']}/messages", json={
        'content': '使用技能生成测试文件，执行前先询问我。', 'request_key': str(uuid4()), 'publication_mode': 'preview'})
    assert response.status_code == 202
    root = response.json()
    with services.engine.connect() as connection:
        schema = connection.exec_driver_sql('SELECT current_schema()').scalar_one()

    def launch():
        generation = services.jobs.get_run(root['id'])['generation']
        result = subprocess.run([
            sys.executable, 'tests/reliability_process.py', schema, services.gateway.ledger.scope,
            str(tmp_path), root['id'], str(generation), 'skill',
        ], capture_output=True, text=True, timeout=150)
        assert result.returncode == 0, result.stderr[-4000:]
        return json.loads(result.stdout)

    first = launch()
    assert first['run']['status'] == 'waiting_input', json.dumps(first, ensure_ascii=False)
    output = tmp_path / 'workspace/outputs/restored.txt'
    assert not output.exists()
    with services.sessions.begin() as session:
        assert session.scalar(select(RunSkillUse).where(RunSkillUse.run_id == root['id'])) is not None
        # 只去掉技能诊断事件，验证RunSkillUse独立承担恢复事实；发布决定等
        # 仍是恢复协议的一部分，生产清理也不会在等待中的任务删除它们。
        removed = session.execute(delete(RunEvent).where(
            RunEvent.run_id == root['id'], RunEvent.kind.in_(['skill_read', 'skill_materialized']),
        ))
        assert removed.rowcount >= 1
    assert services.jobs.publication_intent(root['id']) is not None
    updated = manager.stage([('SKILL.md', b'---\nname: restore-skill\ndescription: new version\n---\nNo original scripts')], expected_revision=1)
    manager.commit(updated['id'])
    assert not (directory / 'restore-skill/scripts/run.py').exists()
    if revoked:
        policy.update(expected_revision=policy.get()['revision'], skill_enabled={'restore-skill': False})
        policy.update(expected_revision=policy.get()['revision'], skill_enabled={'restore-skill': True})
    question = client.get(f"/api/runs/{root['id']}/questions").json()[0]
    answer = client.post(f"/api/runs/{root['id']}/questions/{question['id']}/answer", json={
        'expected_revision': question['revision'], 'kind': 'text', 'value': '继续'})
    assert answer.status_code == 200
    second = launch()
    if revoked:
        assert second['run']['status'] == 'failed', json.dumps(second, ensure_ascii=False)
        assert '撤回' in second['run']['stop_reason']
        assert second['model_requests'] == 0 and not output.exists()
    else:
        assert second['run']['status'] == 'completed', json.dumps(second, ensure_ascii=False)
        assert output.read_text() == 'ff0081'
        assert second['run']['tool_calls'] == 3
