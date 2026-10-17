"""材料与联网工具：按需列出、检索和补读可追溯资料。"""

from __future__ import annotations

from typing import Annotated

from langchain.tools import ToolRuntime, tool
from pydantic import Field

from knowledgeops_agent.harness.runtime.state import RunContext
from knowledgeops_agent.sources.search_config import SearchTimeRange, WebSearchConfig
from knowledgeops_agent.sources.web import WebResearchService


@tool
def list_materials(runtime: ToolRuntime[RunContext]) -> list[dict]:
    """列出本项目已导入材料的 ID 和标题；正文须通过来源片段读取。"""
    return runtime.context.sources.list_sources()


@tool
def list_evidence(runtime: ToolRuntime[RunContext], query: str = "",
                  offset: Annotated[int, Field(ge=0)] = 0,
                  limit: Annotated[int, Field(ge=1, le=50)] = 30) -> dict:
    """分页查询本根任务已经实际读取的引用目录，补足背景中未展示的目录项。

    query用于按标题和已读原文关键词排序，offset用返回的next_offset继续。
    重复读取同版本同范围会合并，不合并不同版本/片段。返回citation、read_tool、
    read_args和observed_range；目录不代表新读取，不增加引用许可。需要核对原文时
    按read_tool/read_args补读，不能把目录当作已看过全部来源内容。
    """
    context = runtime.context
    root = context.jobs.get_run(context.root_id)
    return context.sources.evidence_page(
        context.root_id, query=query, current_source_ids=root["input"]["source_ids"],
        offset=offset, limit=limit,
    )


@tool
def inspect_material(source_id: str, runtime: ToolRuntime[RunContext]) -> dict:
    """获得材料版本、页码、解析状态和可读取片段 ID；没有读取正文的副作用。"""
    return runtime.context.sources.get_source(source_id)


@tool
def read_source(segment_id: str, runtime: ToolRuntime[RunContext]) -> dict:
    """实际读取来源片段，返回正文、固定版本、页码及可直接使用的 citation 链接。"""
    return runtime.context.sources.read_segment(segment_id, root_id=runtime.context.root_id)


def _web_service(context: RunContext) -> WebResearchService:
    """工具可见性之外再次检查可信选项，防止检查点重放绕过仅知识库模式。"""
    root = context.jobs.guard(context.run_id, worker_id=context.worker_id)
    if root["input"]["knowledge_only"]:
        raise PermissionError("network research is disabled in knowledge-only mode")
    # 新运行使用已冻结配置；旧快照缺少此字段时保持旧默认，恢复不重新读YAML。
    settings = WebSearchConfig.from_snapshot((context.resources or {}).get("web_search"))
    return WebResearchService(context.sources, search_config=settings)


@tool
def search_web(
    query: str,
    runtime: ToolRuntime[RunContext],
    count: Annotated[int | None, Field(ge=1, le=8)] = None,
    time_range: SearchTimeRange | None = None,
) -> list[dict]:
    """搜索公开网页以发现来源。摘要不是已读证据，须 read_web 后才能引用。

    query为具体检索词，可用site:限定官网。count省略时用运行配置，指定时须为1至8。
    time_range仅在用户要求近期信息时选择day/week/month/year，否则留空以免漏掉资料。
    搜索引擎筛选不能证明发布日期或正文正确，仍须阅读核对来源。
    """
    return _web_service(runtime.context).search(query, count=count, time_range=time_range)


@tool
def read_web(url: str, runtime: ToolRuntime[RunContext], refresh: bool = False) -> dict:
    """抓取公开网页、保存原文快照并读取首段。remaining_segments 非空时按需 read_source 补读。

    默认复用一小时内的已有快照；确认来源过时可 refresh。禁止私网、任意本地文件和凭据网址。
    """
    return _web_service(runtime.context).read(url, root_id=runtime.context.root_id, refresh=refresh)
