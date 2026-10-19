"""可选模板不能复活强制角色；新目录与旧冻结绑定分别验证。"""

from copy import deepcopy

from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import func, select
from test_api_worker import client as _client
from test_api_worker import services as _services
from test_runtime import setup_run

from knowledgeops_agent.harness.subagents.registry import SubagentRegistry, builtin_definition
from knowledgeops_agent.persistence.models import Run, SubagentDefinition

client = _client
services = _services


def test_empty_directory_does_not_install_a_default_researcher(client, services):
    registry = SubagentRegistry(services.sessions)
    assert client.get('/api/subagents').json() == []
    assert registry.snapshot() == []
    preview = client.get('/api/tools/effective').json()
    task = next(tool for tool in preview['tools'] if tool['name'] == 'task')
    assert not task['available'] and 'no_subagents' in task['unavailable_reasons']
    module, context, _ = setup_run((services.jobs, services.knowledge, services.sources, services.gateway.ledger))
    graph = module.build_agent(context, checkpointer=InMemorySaver())
    assert context.resources['subagents'] == []
    assert 'task' not in graph.nodes['tools'].bound.tools_by_name


def test_template_read_is_not_creation_and_created_role_is_fully_managed(client, services):
    templates = client.get('/api/subagents/templates')
    assert templates.status_code == 200
    template = next(row for row in templates.json() if row['id'] == 'researcher')
    with services.sessions() as session:
        assert session.scalar(select(func.count()).select_from(SubagentDefinition)) == 0
        assert session.scalar(select(func.count()).select_from(Run)) == 0
    response = client.post('/api/subagents', json=template['fields'])
    assert response.status_code == 201, response.text
    created = response.json()
    assert created['id'] != 'builtin-researcher' and not created['builtin']
    assert not created['enabled']
    endpoint = '/api/subagents/' + created['id']
    assert client.get(endpoint).json() == created
    edited = client.patch(endpoint, json={'expected_revision': 1, 'enabled': True, 'display_name': '我的研究者'})
    assert edited.status_code == 200
    assert SubagentRegistry(services.sessions).snapshot() == [edited.json()]
    assert client.patch(endpoint, json={'expected_revision': 2, 'enabled': False}).status_code == 200
    assert SubagentRegistry(services.sessions).snapshot() == []
    assert client.patch(endpoint, json={'expected_revision': 3, 'deleted': True}).status_code == 200
    assert client.get('/api/subagents').json() == []
    assert client.get(endpoint).status_code == 404
    assert len(client.get(endpoint + '/history').json()) == 4
    assert services.gateway.ledger.records() == []


def test_legacy_frozen_role_restores_without_rejoining_the_new_catalog(client, services, monkeypatch):
    import knowledgeops_agent.harness.subagents.registry as definitions
    registry = SubagentRegistry(services.sessions)
    frozen = builtin_definition(1)
    # 模拟升级前已冻结的运行；只在首次冻结使用旧目录，之后从真实保存器读取。
    module, context, _ = setup_run((services.jobs, services.knowledge, services.sources, services.gateway.ledger))
    with monkeypatch.context() as old_version:
        old_version.setattr(definitions, 'active_definitions', lambda session: [deepcopy(frozen)])
        graph = module.build_agent(context, checkpointer=InMemorySaver())
    assert 'task' in graph.nodes['tools'].bound.tools_by_name
    saved = deepcopy(context.resources)
    context.resources = None
    graph = module.build_agent(context, checkpointer=InMemorySaver())
    assert context.resources == saved
    assert registry.validate_binding(frozen) == frozen
    assert 'task' in graph.nodes['tools'].bound.tools_by_name
    assert registry.snapshot() == [] and client.get('/api/subagents').json() == []
