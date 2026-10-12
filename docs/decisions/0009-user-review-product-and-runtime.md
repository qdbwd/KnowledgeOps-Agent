# 0009：用户审查后的产品定位与运行基础补足

- 日期：2026-10-08。
- 状态：保留首轮审查决策历史。下述“待定设计”记录当时状态，已由[决策0010](0010-optimization-plan-handoff.md)与[完整优化计划](../implementation-plan.md)收敛并取代；不代表新增能力已经实现。
- 依据：当前会话对交付结果的审查，以及用户对主动引导的进一步收敛。

## 已确认的产品要求

本应用以 Agent 对话作为主要交互入口。用户决定知识方向、内容范围与最终取舍；Agent 必须默认了解本应用知识库的构建维护任务、内容组织和质量规则。不能让用户每次新建会话重新介绍知识库的用途和维护要求。

对新的学习主题，默认先给出适量、明确范围且有依据的主题框架，说明它是可修订的学习地图，不冒充完备权威目录。已有具体问题应直接回答，不每轮重新规划课程。不能仅凭聊天或一次解释推断用户已经掌握知识。

用户可以全程主导学习内容，这是首要保底路径。更主动的引导、发现遗漏、适时提问和调整路径是增强方向；若流程难以可靠设计，可以暂缓该增强，但不能省略默认加载知识库规则与可靠完成整理维护的基础。联网是获得证据的通用能力，不默认把一个主题转换为自主撰写整本知识手册的任务。

## 本轮明确修正

- 不使用 GitHub CI，移除 workflow，保留开发容器中的必要工程验证。
- 补齐既定自然语言发布契约：真实用户消息可以表达本任务直接保存、仅预览，或确认同会话已展示的具体草稿；与 UI 共用受控业务服务。模型解释意图，程序校验授权来源、范围、版本与幂等。
- 修复引用占位符链路与逐篇正文验收，不能用最终回复的真链接掩盖知识正文中的占位文字。
- 默认向每次 Agent 装配加载本应用领域规则，不把复制的个人规则参考文件当作自动生效的运行机制。
- 保持 DeepSeek 现有同渠道适配，补足真实工具消息往返的离线协议测试。

## 下一轮范围与待定设计

用户提出先收尾当前修正，再用计划模式完成审查并形成优化计划，之后交由长期目标执行的建议；本轮已授权先完成现有修改。后续推荐采用该顺序，最终优化内容仍需审阅。当前修正不是新长期目标，没有自动切换计划模式。下一轮应在聊天中连贯解释产品、运行机制、数据与评估的相互关系，避免只列文件或逐次零散补功能。

用户已要求纳入 Skills 加载、MCP 工具接入、跨会话用户偏好记忆，并改进上下文管理。当前只读核查确认这些扩展尚未完整实现；本轮强制领域规则加载也不等于完整 Skill 系统。

推荐保留现有 create_agent / LangGraph、业务事务服务和统一模型账本，按需复用生态中间件/适配器，而不整体二开 DeerFlow。Skills 的加载与版本、MCP 的连接和许可、记忆的提取/更新/删除、完整请求的上下文预算应作为明确的运行机制，而不是零散追加提示词。

具体记忆写入与冲突策略、外部 MCP 服务清单、有效模型窗口和工作上下文目标仍需形成可执行设计；未验证的配置不作为现有事实。通用代码执行沙盒仍未纳入已批准范围。

## 参考与验证责任

本次核对 Deep Agents 源码版本 `6a3a12bc5b3ac88aeb091e6c77ba81ad4d6732af`（0.7.23）与 DeerFlow 2.0 源码版本 `7797409355cbf257923a81ab9781b706c38f9c3c`，仅用于静态设计与协议对照，未在项目中安装或运行这两个完整应用。

- [Deep Agents Skills](https://docs.langchain.com/oss/python/deepagents/skills)：资源发现与按需加载，关键规则可明确加载。
- [Deep Agents 上下文实现](https://github.com/langchain-ai/deepagents/blob/6a3a12bc5b3ac88aeb091e6c77ba81ad4d6732af/libs/deepagents/deepagents/middleware/summarization.py)：模型 profile 与 token 策略，不把其默认百分比直接作为本项目结论。
- [DeerFlow Lead Agent](https://github.com/bytedance/deer-flow/blob/7797409355cbf257923a81ab9781b706c38f9c3c/backend/packages/harness/deerflow/agents/lead_agent/agent.py)：技能、记忆、持久上下文与工具机制的实际装配。
- [DeerFlow DeepSeek 适配](https://github.com/bytedance/deer-flow/blob/7797409355cbf257923a81ab9781b706c38f9c3c/backend/packages/harness/deerflow/models/patched_deepseek.py)：同样对发送路径作协议补齐，不能假定任意 LangChain 版本已自动兼容。
- [DeerFlow引用提示词](https://github.com/bytedance/deer-flow/blob/7797409355cbf257923a81ab9781b706c38f9c3c/backend/packages/harness/deerflow/agents/lead_agent/prompt.py#L632-L693)与[链接组件](https://github.com/bytedance/deer-flow/blob/7797409355cbf257923a81ab9781b706c38f9c3c/frontend/src/components/workspace/messages/markdown-link.tsx#L89-L125)：网页主路径要求模型输出完整 `[citation:Title](URL)`，前端再渲染徽标；不是无URL标记自动获得来源。本项目保留指向固定证据版本的标准Markdown链接，并补足格式约定、校验与逐篇正文验收，没有照搬外部网页URL来替换持久证据。
- [LangChain MCP](https://docs.langchain.com/oss/python/langchain/mcp)、[长期记忆](https://docs.langchain.com/oss/python/langchain/long-term-memory)：现有接口与应用需要自行决定的生命周期/业务策略分别核对。

历史 22/24 与 36 次开关对照原样保留。后者没有实际子任务，只证明配置可运行，不能作为多 Agent 协作收益的比较；真实学习交互、规则加载、自然语言授权与修订后引用检查需要自己的验证证据。实际改动、结果与遗留统一更新 progress 和 development-issues，不另建一份竞争实施计划。
