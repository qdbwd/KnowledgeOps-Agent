"""独立进程会话池验证器；从测试schema读取同一连接，stdout仅输出协议验证结果。"""

import json
import os
import sys

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from knowledgeops_agent.harness.mcp.registry import MCPRegistry
from knowledgeops_agent.harness.mcp.sessions import MCPSessionManager

engine = create_engine(os.environ['MCP_TEST_DATABASE_URL'],
                       connect_args={'options': '-csearch_path=' + os.environ['MCP_TEST_SCHEMA']})
registry = MCPRegistry(sessionmaker(engine, expire_on_commit=False))
binding = registry.get(os.environ['MCP_TEST_CONNECTION'])


async def call(manager):
    async with manager.lease('test-scope', binding) as lease:
        tools = await lease.adapter.list_tools(cache_mode='bypass')
        tool = next(item for item in tools if item.name == 'counter')
        result = await tool.ainvoke({'name': 'counter', 'args': {}, 'id': 'multiworker', 'type': 'tool_call'})
        return {'session_id': lease.session_id, 'reused': lease.reused,
                'result': result.artifact['structured_content']}


try:
    with MCPSessionManager(is_valid=lambda scope, current: registry.is_binding_active(current),
                           sweep_seconds=0.1) as manager:
        print(json.dumps({'ready': True}), flush=True)
        for line in sys.stdin:
            if line.strip() == 'close':
                break
            try:
                print(json.dumps(manager.run(call, manager)), flush=True)
            except Exception as error:
                print(json.dumps({'error': type(error).__name__}), flush=True)
finally:
    engine.dispose()
