# 任务连续性参考与适配

固定源码：[DeerFlow提交127c2c220c30d875b2995e95608b98d86db6bc13](https://github.com/bytedance/deer-flow/tree/127c2c220c30d875b2995e95608b98d86db6bc13/backend/packages/harness/deerflow/agents/task_continuity)。适用MIT许可见LICENSE。

- `task_continuity_state.py`：摘取state.py，含笔记规范化、归并、Overwrite保护和历史引用规范化，仅按本项目规范格式化。
- `task_note_admission.py`：从tools.py摘取形状检查和并行新增容量分配函数，仅去掉DeerFlow专属Runtime类型注解、改为本地导入。
- `history_terms.py`：从archive.py摘取中文双字词/英文关键词函数，仅格式化并补充局部变量类型注解。

本项目接入层复用LangChain ToolRuntime/Command/中间件和现有PG检查点。未复制DeerFlow私有运行目录、SQLite服务或整个harness。历史存储适配为现有PostgreSQL中的不可变记录/批次与GIN关键词数组索引；可读范围仍由当前检查点引用及会话scope共同约束。保存可见原文全文供分页，过滤隐藏推理和临时上下文；旧摘要原文产物仅按冻结快照许可导入索引。工作笔记与来源指针随会话延续，预算/授权不继承。

真实联调发现适配工具遗漏了上游`_task_note`的`invalid_note.limits`反馈：模型使用中文内部键时只收到笼统错误，无法确定该改哪个字段。现保留原上游限制提示，并在本地LangChain工具参数中用Pydantic Field公开同一组键格式、正文长度和来源数量约束；常量与上游校验共享，正文仍可用中文，没有放宽校验。Schema及真实图中的错误纠正链路有回归，原始失败保留，详见DEV-077。

提示组织借鉴[DurableContextMiddleware](https://github.com/bytedance/deer-flow/blob/127c2c220c30d875b2995e95608b98d86db6bc13/backend/packages/harness/deerflow/agents/middlewares/durable_context_middleware.py)：系统规则与临时历史资料分开。未复制其Goal/委派/技能持久账本，继续复用本项目已有模块。参考测试包括原话补读、旧检查点不可见未来、角色过滤、超长来源、并行笔记以及同一真实Graph搜索—读取—记笔记；本项目另验证跨run快照和统一工具撤回。

2026-10-09再次核对最新main为bec63a6107b634651eb8e304f4bb04393bd81901，相对固定参考提交新增3个提交、19份文件变化，任务连续性、DurableContextMiddleware、MemoryMiddleware及对应测试没有变化。因此不重复摘取代码；差异核对记录见artifacts/verification/optimization/context-upstream-reference.json。MemoryMiddleware负责轮末向长期记忆管理器提交更新，不能把它当成每次模型调用前的完整上下文选择器；本项目的长期偏好仍由原偏好服务管理。
