"""技能资源的真实边界：冻结内容、按需加载、只读工具，不暴露宿主文件。"""

import importlib

import pytest
from langchain.agents import create_agent
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from pydantic import Field


@pytest.fixture
def bundle():
    return {
        "/skills/learning/SKILL.md": "---\nname: learning\ndescription: Organize a learning dialogue\n---\n# 学习\n先建立主题框架，再围绕用户的问题推进。\n",
        "/skills/learning/examples.md": "第一行\n第二行\n第三行",
    }


def test_skill_catalog_is_loaded_from_snapshot_and_only_read_tool_is_exposed(bundle):
    module = importlib.import_module("knowledgeops_agent.harness.skills.resources")
    backend, middlewares = module.build_skill_middleware(bundle)
    tools = [tool for middleware in middlewares for tool in getattr(middleware, "tools", [])]
    assert [tool.name for tool in tools] == ["read_file"]

    class ReadingModel(FakeMessagesListChatModel):
        """只替换模型响应；图、中间件、运行时注入和文件读取均为真实实现。"""

        received: list = Field(default_factory=list)

        def bind_tools(self, tools, **kwargs):
            return self

        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            self.received.append(messages)
            return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)

    model = ReadingModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "read_file",
                        "args": {
                            "file_path": "/skills/learning/examples.md",
                            "offset": 1,
                            "limit": 1,
                        },
                        "id": "skill-read",
                    }
                ],
            ),
            AIMessage(content="读取完成"),
        ]
    )
    graph = create_agent(model=model, middleware=middlewares)
    state = graph.invoke({"messages": [HumanMessage("读取技能示例的第二行")]})
    # 上游将技能元数据标为内部状态；验收模型实际收到目录，不依赖图的输出schema。
    assert "Organize a learning dialogue" in "\n".join(str(m.content) for m in model.received[0])
    result = next(message for message in state["messages"] if isinstance(message, ToolMessage))
    assert result.status == "success"
    assert "第二行" in result.content
    assert "第三行" not in result.content
    assert backend.read_receipts[-1]["path"] == "/skills/learning/examples.md"


def test_frozen_backend_does_not_follow_mutated_source_or_read_other_paths(bundle):
    module = importlib.import_module("knowledgeops_agent.harness.skills.resources")
    backend, _ = module.build_skill_middleware(bundle)
    bundle["/skills/learning/examples.md"] = "被修改的源文件"
    assert (
        backend.download_files(["/skills/learning/examples.md"])[0].content
        == "第一行\n第二行\n第三行".encode()
    )
    assert backend.read("/etc/passwd").error
    assert backend.read("/skills/learning/../../etc/passwd").error
    assert backend.write("/skills/learning/examples.md", "overwrite").error
    assert backend.delete("/skills/learning/examples.md").error


def test_resource_hash_changes_only_when_content_changes(bundle):
    module = importlib.import_module("knowledgeops_agent.harness.skills.resources")
    first, _ = module.build_skill_middleware(bundle)
    second, _ = module.build_skill_middleware(dict(reversed(list(bundle.items()))))
    assert first.content_hash == second.content_hash
    bundle["/skills/learning/examples.md"] += "\n第四行"
    changed, _ = module.build_skill_middleware(bundle)
    assert changed.content_hash != first.content_hash
