# DDGS 搜索配置参考与适配

2026-10-10核对当时DeerFlow最新main，固定在[ee0ba401a6fa566459b85517ead136ebae908b53](https://github.com/bytedance/deer-flow/tree/ee0ba401a6fa566459b85517ead136ebae908b53)。本模块适用同目录MIT LICENSE。

直接摘取[ddg_search/tools.py](https://github.com/bytedance/deer-flow/blob/ee0ba401a6fa566459b85517ead136ebae908b53/backend/packages/harness/deerflow/community/ddg_search/tools.py)的六个配置纯函数和相关常量至`ddg_search_options.py`：规范化后端名称、识别Wikipedia后端、修正`wt-wt`及日/韩/繁中地区别名。去掉其应用配置、工具注册、搜索执行依赖，调整格式和一个说明文字；可执行函数AST与固定原文逐一比对。

设计参考是“部署配置决定后端/地区/默认条数，工具调用决定查询词和可选时间范围”。相应上游测试为[test_ddg_search_tools.py](https://github.com/bytedance/deer-flow/blob/ee0ba401a6fa566459b85517ead136ebae908b53/backend/tests/test_ddg_search_tools.py)和[test_web_search_time_range.py](https://github.com/bytedance/deer-flow/blob/ee0ba401a6fa566459b85517ead136ebae908b53/backend/tests/test_web_search_time_range.py)，核对参数实际传给SDK、地区修正和模型Schema中的可选时间枚举。本地没有安装运行DeerFlow测试套件。

项目适配集中在`web_search_config.py`，没有复制DeerFlow整套配置系统：

- 本项目原来已用DDGS，继续直接依赖库；不新增MCP搜索服务或收费搜索渠道。
- YAML用现有PyYAML/Pydantic加载校验，非法字段/后端/条数明确报错，避免DDGS对拼错后端静默退回auto。默认保留原后端、中文地区推断、5条结果和15秒SDK超时；每次最多8条。
- 配置的`max_results`是默认值，Agent可在上限内指定本次`count`；不是DeerFlow配置值强制覆盖工具参数的语义。
- 本项目锁定DDGS 9.16.0，上游注释针对9.14.1。根据本地引擎实际`build_payload`，时间过滤支持Brave、DuckDuckGo、Google、Startpage、Yahoo；Startpage新增`with_date`，不能沿用旧列表。明确配置的后端没有时间过滤能力时返回可纠正错误，不像上游那样自动换到配置外的后端。
- 沿用原资源快照冻结配置，恢复和子任务共享根任务版本；旧快照没有该字段时明确使用改动前的默认值，不回写历史。配置不等于工具许可，统一工具管理仍负责开关和撤回。
- 网页下载、正文抽取和固定版本证据仍走项目已有服务；本轮未新增重排、向量检索或语义核验。搜索结果与有据可查的正文仍然分开。

Deep Agents的[研究示例](https://docs.langchain.com/oss/python/deepagents/quickstart)让应用提供搜索工具，当前没有可替代本项目DDGS配置的统一现成入口，因此只保留它已接入的Agent SDK能力。验证范围及真实网络波动见项目进展和`artifacts/verification/optimization/search-configuration-20261010/`。
