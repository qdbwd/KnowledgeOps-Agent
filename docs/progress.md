# KnowledgeOps Agent 项目进展

> 本文记录当前代码和可核验的工程进展。它不是学习打卡表；没有代码、测试或运行记录支持的内容不标记为完成。

## 当前快照

- 更新日期：2026-10-08
- 项目路径：`D:\codes\KnowledgeOps Agent`
- 当前阶段：业务主代码保留 M0；用户授权的独立学习小程序位于 exercises/01_agent_loop/，不作为业务 M1 完成证据。
- 真实模型调用：未启用
- 项目知识库：此前复制的 43 份 Markdown 和导入清单已清理，当前无业务数据；工具、数据库和索引尚未实现，存储技术未选定。
- 个人知识库：保持独立并作为设计参考；Agent 不操作源目录、不隐式同步，不自动恢复已删除的副本。
- Web 后端/前端：用户已确认需要用于对话和可视化操作的简单 Web UI，具体方案待设计，尚未实现。
- 开发环境：已确认本项目独立容器组；技术栈与服务待选定，容器尚未配置，不复用现有共享开发容器。
- Git：本地 `main` 分支已初始化，尚无提交或 remote；用户已创建 GitHub 私有仓库，本次仅核对本地状态，未访问远端或推送。

## 已完成

### M0 合同骨架

- 使用 LangGraph 建立最小图；
- 定义请求、任务类型、剩余步数、证据、引用、工具历史、错误和终态字段；
- 定义确定性的步数消耗和路径策略；
- 增加 M0 合同测试和策略测试；
- 记录当前能力边界：没有模型决策、工具调用、checkpoint、真实检索或评估 Adapter。

## 2026-09-25：独立单 Agent 学习小程序

- 用户已确认：实现层学习、练手与调试全部使用独立小程序，放在实战项目的 exercises/ 下。
- 已实现标准消息、LangGraph 状态与条件边、只读教学工具、离线脚本/真实模型适配、错误反馈、预算终止、CLI 和本地轨迹。
- 真实模式仅提供接入，尚未发送真实模型请求；脚本结果不证明模型自主决策能力。
- 开发与验证环境：Windows 本地 Python 3.11，练习独立 uv 环境。
- 验证结果：13 项 pytest 测试通过（包括在线客户端配置的 mock 测试），Ruff 检查和格式检查通过（8 个 Python 文件）。
- 离线命令行实跑：正常情节 4 次决策、5 次工具派发，终态 answered；轨迹位于 `exercises/01_agent_loop/runs/offline-20260925.json`（被 Git 忽略）。
- 实际环境：Windows Python 3.11.14；LangGraph 1.1.9、langchain-core 1.6.5、langchain-openai 1.6.6，完整依赖见练习 uv.lock。
- 检查范围：正常读取/补读、工具错误反馈与恢复、未知工具、模型异常、消息协议错误、两类预算、运行隔离、CLI/轨迹；未验证真实服务商接口和模型决策质量。
- 首轮测试受 Windows 沙箱下 pytest 临时目录权限影响；改用当前用户权限和本次专属临时目录后，13 项全部通过。未通过修改业务代码来绕过该权限问题。
- 文档入口：[练习说明](../exercises/01_agent_loop/README.md)、[边界决策](decisions/0003-isolated-agent-learning-exercises.md)。

## 2026-09-25：练习环境与 Miniconda 分离

- 用户明确要求使用本机独立 uv，不再调用 Miniconda 中的 uv。
- 独立入口：`C:\Users\cyq\.local\bin\uv.exe`，版本仍为 0.11.7，来自现有原生可执行文件的同版本复制；独立运行不需要 Miniconda 的 Python。未修改 Miniconda 安装或持久化 PATH。
- 基础解释器：uv 管理的 CPython 3.11.14，位于 `C:\Users\cyq\AppData\Roaming\uv\python\cpython-3.11.14-windows-x86_64-none`。安装时未加入 Python 命令入口或 Windows 注册表。
- 为练习新增 `.python-version` 并设置 `python-preference = "only-managed"`；原地重建 `.venv`，避免未来自动选择 conda 解释器。
- 核验：锁文件 SHA-256 保持不变；迁移前后 50 个已安装发行包及版本完全一致；`sys.base_prefix` 和 `sys.path` 不含 Miniconda。
- Windows 本地验证：从测试进程 PATH 排除 Miniconda 后，13 项 pytest 测试通过；离线 CLI 正常结束，4 次决策、5 次工具派发，终态 `answered`。未调用真实模型。
- 范围：仅迁移独立学习练习；主项目业务代码、依赖及其他项目环境未调整。

## 2026-09-25：自动读取在线模型配置

- 用户指定 `deepseek-flash`，并要求代码读取已经填写的 URL 和 API key，无需每次在终端设置。
- 新增 `config.py`，在 online 模式下读取本项目根目录与练习目录的 .env；支持 BASE_URL/API_KEY 和 OPENAI_* 名称，进程变量优先，不依赖启动工作目录，不修改全局环境。
- 在根目录 .env 追加 `AGENT_MODEL=deepseek-flash`，并按用户要求将字段统一为 OPENAI_BASE_URL/OPENAI_API_KEY，原有 URL 和密钥值保持不变；同步无密钥的 .env.example 和练习说明。
- 新增直接依赖 python-dotenv，锁定版本 1.2.3；其他已有包版本保持不变。
- Windows 本地验证：16 项 pytest 测试通过，包含在线 CLI 的 mock 闭环、配置覆盖及默认离线模式不加载配置；Ruff 检查与格式检查通过（11 个 Python 文件）。
- 当前配置可读取并传入 mock 客户端；没有发送真实模型请求，服务商兼容性和真实工具调用效果尚未验证。
- [配置决策](decisions/0004-exercise-local-model-config.md)。

## 2026-09-28：练习源码阅读指南

- 按用户要求，将前期源码讲解及后续接口辨析整理为[源码阅读与实现要点](../exercises/01_agent_loop/CODE_WALKTHROUGH.md)，放在对应练习目录。
- 正文直接嵌入六个运行相关 Python 模块和 pyproject.toml 的完整内容，解释工具、模型适配、消息与状态、节点及条件边、错误和预算、配置路径、CLI 与 JSON 轨迹；保留流程图和 A/B 材料的逐轮示例。
- 补充个人学习与面试复习建议，区分原理、常用实现和可查阅的细节；不将学习建议变成业务开发需求或用户掌握进度。
- Windows 本地仅做文档结构检查、嵌入源码一致性检查、正文审读及官方资料核对；未运行 Agent、测试、依赖安装或真实模型请求。
- 业务代码、练习代码与依赖未修改；个人知识库未新增正文副本。同步练习 README 和 PROJECT_CONTEXT 的阅读入口。

## 2026-09-28：保留业务应用层参考项目

- 按用户要求，在[项目上下文](../PROJECT_CONTEXT.md)记录 Basic Memory、Khoj、Onyx、RAGFlow 及各自的参考方向，留待具体业务设计阶段使用。
- 当前继续学习通用 Agent 机制；本次仅更新参考记录，不作技术选型或新增功能实现。

## 2026-10-07：清理个人知识库初始副本

- 按用户明确要求，删除 `data/knowledge_base/documents/` 下的 43 份 Markdown 及 `data/knowledge_base/import_manifest.json`，项目知识库恢复为无业务数据状态。
- 删除前逐一核对导入清单、文件数量、大小和 SHA-256，43 份文件均与原始导入记录一致；确认目标位于本项目内，路径及目录内容不存在链接或联接点。
- 删除后核对：`data/` 中仅剩 `knowledge_base/README.md` 这一说明文件，无业务文档、数据库或索引文件。空目录删除命令被自动审批拦截，工具仅返回 `blocked by policy`，因此空目录暂留。
- 同步项目入口、工程规则、实施草案和数据目录说明，记录[清理决策](decisions/0005-remove-personal-kb-snapshot.md)；后续初始材料另行确定，不自动重新复制个人知识库。
- 本次在 Windows 本地完成文件清理与静态核对；个人知识库原件、项目代码、练习、配置与依赖保留，未运行应用、测试、模型调用或安装依赖。

## 2026-10-08：复制知识库规则参考并核对仓库初始化

- 按用户授权，将个人知识库根目录的 `AGENTS.md` 和 `outputs/_meta/知识库设计与决策.md` 复制至 `docs/references/personal-knowledge-base/`，分别使用 `maintenance-rules.source.md` 和 `design-decisions.source.md`，并补充来源、使用边界和 SHA-256 校验记录。
- 两份副本与源文件逐字节一致；仅作为设计参考，不替代项目工程规则，不恢复已删除的知识正文，不写入项目业务知识库。
- 同步此前已确认的独立容器组与简单 Web UI 要求，区分设计决定和实际建设状态；未决定数据库、服务清单或框架具体用法。
- Windows 本地只读 Git 核对：仓库位于本项目根目录，分支为 `main`，无提交、无已跟踪文件、无 remote；`.env`、主项目与练习的 `.venv/`、运行产物受忽略规则保护。
- 根目录已有 `pyproject.toml`、源码、测试和项目文档；练习有独立 `uv.lock`，主项目无锁文件、Dev Container 或 CI 配置。上述核对不是代码质量或可运行性验收。
- 用户提供的截图显示已创建 GitHub 私有仓库 `qdbwd/KnowledgeOps-Agent`；本次未验证远端内容，未执行 `git add`、提交、配置 remote 或推送，也未运行测试、应用或安装依赖。

## 当前代码证据

| 位置 | 当前作用 |
|---|---|
| `src/knowledgeops_agent/state.py` | Agent 状态、任务类型、审批状态和终态 |
| `src/knowledgeops_agent/graph.py` | normalize 后进入 M0 终止节点 |
| `src/knowledgeops_agent/policies.py` | 路径、写入策略和步数规则 |
| `tests/test_graph_contract.py` | M0 图合同 |
| `tests/test_policies.py` | 确定性策略 |

## 暂停中的 M1 草案（非当前执行任务）

按以下顺序实现，每次保持一个主要变化：

1. 定义 `ToolSpec`、结构化工具结果和工具错误；
2. 增加脚本化决策器，支持 `answer`、`tool_call`、`finish`；
3. 实现 `decide → execute → observe → validate → decide/finish` 循环；
4. 增加最大步数、连续重复调用、非法工具选择和预算耗尽终态；
5. 增加轨迹事件和 CLI/JSONL 运行入口；
6. 用内存假工具和测试夹具验证 ReAct，不调用真实模型；
7. 再在同一 Runtime 上增加 Plan-and-Execute 的计划字段和重规划。

M1 完成标准：一个不依赖真实模型的脚本化 Agent 能完成至少一次工具调用并安全终止，工具失败和预算耗尽有可观测的结构化结果，测试能验证关键路径。

## 早期后续里程碑建议（尚未作为最终计划确认）

| 阶段 | 产物 | 完成证据 |
|---|---|---|
| M2 | 项目知识库、读写工具、索引和 Native RAG | 固定夹具上的问答、引用和维护测试 |
| M3 | checkpoint、人审、版本冲突和回滚 | 暂停/恢复/拒绝/应用测试 |
| M4 | GraphRAG 可选工具 | 与 Native RAG 的同条件对照 |
| M5 | 评估 Adapter | 协议、失败、超时、coverage 和事实映射测试 |
| M6 | 可选 FastAPI/SSE | HTTP 只做适配，核心测试不依赖 Web |

## 维护规则

- 每次编码前先读 `PROJECT_CONTEXT.md` 和与范围对应的设计文档；
- 设计边界变化先更新 `docs/decisions/`，再改代码；
- 阶段变化、测试结果和未完成原因及时写入本文；
- 不把“计划实现”写成“已实现”；
- 不把个人知识库的任务进度自动同步到本项目；
- 不以单次成功、接口 200 或空测试证明 Agent 质量。
