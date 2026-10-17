"""搜索配置合同：离线检查真实工具/图接线，搜索结果质量另用真实网络核对。"""

import importlib
import json
from dataclasses import replace
from unittest.mock import Mock

import pytest
from langchain_core.messages import AIMessageChunk, HumanMessage, ToolMessage
from langchain_core.utils.function_calling import convert_to_openai_tool
from langgraph.checkpoint.memory import InMemorySaver
from test_runtime import runtime_services as _runtime_services
from test_runtime import setup_run

runtime_services = _runtime_services


def config_module():
    return importlib.import_module("knowledgeops_agent.sources.search_config")


def test_yaml_controls_ddgs_options_and_keeps_search_separate_from_evidence(tmp_path, monkeypatch):
    """配置走实际加载→服务→DDGS参数路径；不把搜索摘录变成已读证据。"""
    config = config_module()
    path = tmp_path / "search.yaml"
    path.write_text(
        "backend: [duckduckgo, brave]\nregion: us-en\nsafesearch: 'off'\n"
        "max_results: 3\ntimeout_seconds: 12\n",
        encoding="utf-8",
    )
    options = config.load_web_search_config(path)
    web = importlib.import_module("knowledgeops_agent.sources.web")
    client = Mock()
    client.text.return_value = [
        {"title": "Python", "href": "https://docs.python.org/", "body": "discovery only"}
    ]
    factory = Mock(return_value=client)
    monkeypatch.setattr(web, "DDGS", factory)
    sources = Mock()
    service = web.WebResearchService(sources, search_config=options)
    found = service.search("Python release", time_range="week")
    factory.assert_called_once_with(timeout=12)
    client.text.assert_called_once_with(
        "Python release",
        backend="duckduckgo,brave",
        region="us-en",
        safesearch="off",
        max_results=3,
        timelimit="w",
    )
    assert found[0]["status"] == "not_read" and "evidence_id" not in found[0]
    sources.import_text.assert_not_called()


@pytest.mark.parametrize(
    "setting",
    [
        "backend: [typo]",
        "backend: [' ']",
        "region: wrong",
        "safesearch: unknown",
        "max_results: true",
        "max_results: 9",
        "timeout_seconds: 0",
        "unknown_setting: 5",
    ],
)
def test_bad_configuration_fails_clearly_instead_of_silently_switching_engine(tmp_path, setting):
    path = tmp_path / "search.yaml"
    path.write_text(setting, encoding="utf-8")
    with pytest.raises(ValueError, match="web_search"):
        config_module().load_web_search_config(path)


@pytest.mark.parametrize(
    "time_range,limit", [("day", "d"), ("week", "w"), ("month", "m"), ("year", "y")]
)
def test_time_filter_only_uses_engines_that_implement_it(time_range, limit):
    config = config_module().WebSearchConfig(backend="wikipedia,startpage,duckduckgo")
    kwargs = config.search_kwargs("recent Python", time_range=time_range)
    # 本项目锁定DDGS 9.16，Startpage已实现时间过滤，不能照抄上游9.14的能力表。
    assert kwargs["backend"] == "startpage,duckduckgo"
    assert kwargs["timelimit"] == limit
    with pytest.raises(ValueError, match="time_range"):
        config_module().WebSearchConfig(backend="wikipedia").search_kwargs(
            "Python", time_range="week"
        )


def test_defaults_language_and_count_contract_match_actual_model_schema():
    module = config_module()
    settings = module.load_web_search_config()
    assert settings.search_kwargs("Python") == {
        "backend": "brave,duckduckgo,startpage,yahoo",
        "region": "wt-wt",
        "safesearch": "moderate",
        "max_results": 5,
    }
    assert settings.search_kwargs("Python 中文文档")["region"] == "cn-zh"
    auto = module.WebSearchConfig(backend="auto")
    assert auto.search_kwargs("Python")["region"] == "us-en"
    assert (
        module.WebSearchConfig(backend="auto", region="tw-tzh").search_kwargs("Python")["region"]
        == "tw-zh"
    )
    assert settings.search_kwargs("Python", count=2)["max_results"] == 2
    runtime = importlib.import_module("knowledgeops_agent.harness.runtime.agent")
    schema = convert_to_openai_tool(runtime.search_web)["function"]["parameters"]
    enum = next(
        item["enum"] for item in schema["properties"]["time_range"]["anyOf"] if "enum" in item
    )
    assert set(enum) == {"day", "week", "month", "year"}
    assert "time_range" not in schema.get("required", [])


def test_run_freezes_search_config_and_legacy_snapshot_does_not_use_new_defaults(
    runtime_services, monkeypatch
):
    config = config_module()
    resources = importlib.import_module("knowledgeops_agent.harness.runtime.resources")
    module, context, _ = setup_run(runtime_services)
    monkeypatch.setattr(
        resources,
        "load_web_search_config",
        lambda: config.WebSearchConfig(backend="duckduckgo", max_results=2),
    )
    service = resources.ResourceService(context.jobs.sessions)
    first = service.ensure(context)
    monkeypatch.setattr(
        resources, "load_web_search_config", lambda: pytest.fail("must use frozen options")
    )
    assert service.ensure(context)["web_search"] == first["web_search"]
    child = context.jobs.start_child(context.root_id, "只读核对", operation_key="search-settings")
    assert service.ensure(replace(context, run_id=child["id"]))["web_search"] == first["web_search"]
    # 后续新任务读取新的配置；更改文件不影响已冻结的根任务或子任务。
    monkeypatch.setattr(
        resources,
        "load_web_search_config",
        lambda: config.WebSearchConfig(backend="brave", max_results=4),
    )
    _, next_context, _ = setup_run(runtime_services)
    assert service.ensure(next_context)["web_search"]["backend"] == "brave"
    assert service.ensure(context)["web_search"] == first["web_search"]
    context.resources = first
    source_tools = importlib.import_module("knowledgeops_agent.sources.tools")
    assert source_tools._web_service(context).search_config.backend == "duckduckgo"
    context.resources = {key: value for key, value in first.items() if key != "web_search"}
    assert source_tools._web_service(context).search_config.backend == "brave,duckduckgo,startpage,yahoo"


def test_actual_graph_routes_search_arguments_and_result_without_model_fee(
    runtime_services, monkeypatch
):
    """真实create_agent和数据库，只有模型/外网替身；证明新参数确实到达搜索库。"""
    config = config_module()
    resources = importlib.import_module("knowledgeops_agent.harness.runtime.resources")
    web = importlib.import_module("knowledgeops_agent.sources.web")
    module, context, _ = setup_run(runtime_services, mode="ask")
    monkeypatch.setattr(
        resources,
        "load_web_search_config",
        lambda: config.WebSearchConfig(backend="duckduckgo", max_results=2),
    )
    client = Mock()
    client.text.return_value = [
        {"title": "Python", "href": "https://docs.python.org/", "body": "Python documentation"}
    ]
    monkeypatch.setattr(web, "DDGS", Mock(return_value=client))

    class SearchProvider:
        def bind_tools(self, tools):
            return self

        def stream(self, messages, **kwargs):
            results = [message for message in messages if isinstance(message, ToolMessage)]
            if not results:
                yield AIMessageChunk(
                    content="",
                    tool_call_chunks=[
                        {
                            "name": "search_web",
                            "args": json.dumps({"query": "Python release", "time_range": "month"}),
                            "id": "search-config-call",
                            "index": 0,
                        }
                    ],
                )
            else:
                assert json.loads(results[-1].content)[0]["status"] == "not_read"
                yield AIMessageChunk(content="已找到候选网页，尚未读取正文。")

    context.gateway.client = SearchProvider()
    graph = module.build_agent(context, checkpointer=InMemorySaver())
    state = graph.invoke(
        {"messages": [HumanMessage("找最近的Python资料")]},
        {"configurable": {"thread_id": context.run_id}},
        context=context,
    )
    assert state["messages"][-1].text == "已找到候选网页，尚未读取正文。"
    client.text.assert_called_once_with(
        "Python release",
        backend="duckduckgo",
        region="wt-wt",
        safesearch="moderate",
        max_results=2,
        timelimit="m",
    )
