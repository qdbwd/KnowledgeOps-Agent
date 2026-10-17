"""主动压缩走真实队列、账本、worker和快照，只替换付费供应商。"""

from uuid import uuid4

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from test_api_worker import client as _client
from test_api_worker import services as _services

from knowledgeops_agent.app.runs.worker import execute_run
from knowledgeops_agent.harness.context.continuation import save_snapshot
from knowledgeops_agent.harness.context.manual import enqueue, status
from knowledgeops_agent.harness.context.usage import context_usage
from knowledgeops_agent.persistence.models import Run

client = _client
services = _services


def seed(services):
    services.gateway.enabled = True
    chat = services.jobs.create_conversation()
    run = services.jobs.enqueue(chat['id'], '历史讨论', request_key=str(uuid4()))
    services.jobs.claim('seed', run_id=run['id'], generation=run['generation'])
    messages = []
    for index in range(6):
        messages.extend([HumanMessage(f'旧讨论{index}：' + '需要保留约束与来源。' * 300, id=f'u{index}'),
                         AIMessage('已记录', id=f'a{index}')])
    from langchain_core.messages import message_to_dict

    packet = services.gateway.request_packet([SystemMessage('保留来源，不擅自发布。'), *messages])
    reservation = services.gateway.ledger.reserve(run['id'], run['id'], 'agent', packet)
    services.gateway.ledger.finish(reservation, response={'content': '已记录'}, usage=None)
    services.jobs.finish(run['id'], 'seed', continuation={'messages': [message_to_dict(m) for m in messages]})
    return chat, run


class SummaryProvider:
    def invoke(self, messages, **kwargs):
        return AIMessage('早期讨论要求保留约束与来源，不擅自发布。')


def test_manual_worker_compacts_once_and_next_run_inherits_without_fake_chat(services):
    chat, prior = seed(services)
    services.gateway.client = SummaryProvider()
    original_chat = services.jobs.messages(chat['id'])
    pending = enqueue(services.jobs, services.gateway, chat['id'], 'compact-once')
    assert enqueue(services.jobs, services.gateway, chat['id'], 'compact-once')['id'] == pending['id']
    assert not status(services.jobs, services.gateway, chat['id'])['available']
    next_run = services.jobs.enqueue(chat['id'], '继续讨论', request_key='after-compact')
    assert services.jobs.claim('out-of-order', run_id=next_run['id'], generation=next_run['generation']) is None
    finished = execute_run(pending['id'], pending['generation'], services=services, worker_id='compact-worker')
    assert finished['status'] == 'completed', finished
    assert finished['outcome']['kind'] == 'context_compacted', finished
    assert services.jobs.messages(chat['id'])[:-1] == original_chat
    records = services.gateway.ledger.records(root_id=pending['id'])
    assert records and all(row['purpose'] == 'context_summary' for row in records)
    assert execute_run(pending['id'], pending['generation'], services=services) is None
    measured = context_usage(services.sessions, services.gateway, chat['id'])
    assert measured['basis'] == 'manual_compaction_estimate'
    assert measured['percentage'] is not None
    services.jobs.claim('next-worker', run_id=next_run['id'], generation=next_run['generation'])
    snapshot = services.jobs.continuation(next_run['id'])
    assert snapshot['run_id'] == pending['id']
    assert snapshot['content']['summary']
    assert len(snapshot['content']['messages']) < 12
    assert snapshot['content']['messages'][-1]['data']['id'] == 'a5'


def test_summary_failure_does_not_publish_a_replacement_snapshot(services):
    chat, prior = seed(services)

    class Failing:
        def invoke(self, *args, **kwargs):
            raise ConnectionError('offline controlled failure')

    services.gateway.client = Failing()
    pending = enqueue(services.jobs, services.gateway, chat['id'], 'compact-failure')
    finished = execute_run(pending['id'], pending['generation'], services=services, worker_id='compact-worker')
    assert finished['status'] == 'failed'
    next_run = services.jobs.enqueue(chat['id'], '继续', request_key='after-failure')
    services.jobs.claim('next-worker', run_id=next_run['id'], generation=next_run['generation'])
    assert services.jobs.continuation(next_run['id'])['run_id'] == prior['id']


def test_http_refuses_disabled_or_active_compaction_without_model_calls(client, services):
    chat, _ = seed(services)
    services.gateway.enabled = False
    path = f"/api/conversations/{chat['id']}/context-compaction"
    assert client.get(path).json()['available'] is False
    assert client.post(path, json={'request_key': 'off'}).status_code == 409
    services.gateway.enabled = True
    services.jobs.enqueue(chat['id'], '普通待执行任务', request_key='pending')
    assert client.post(path, json={'request_key': 'busy'}).status_code == 409
    assert client.get(path).json()['available'] is False


def test_cancelled_compaction_cannot_publish_snapshot(services):
    chat, _ = seed(services)
    pending = enqueue(services.jobs, services.gateway, chat['id'], 'cancelled')
    with services.sessions.begin() as session:
        row = session.get(Run, pending['id'])
        row.status = 'cancelled'
        assert save_snapshot(session, row, {'messages': [{'type': 'human', 'data': {'content': 'wrong'}}]}) is None
