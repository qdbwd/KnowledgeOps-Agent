# 0024 Docker 启动模式、部署资源与页面布局

状态：2026-10-12 用户审查追加，按实际验证结果交付。

用户希望 Docker 快速启动区分运行与开发，部署配置不藏在业务源码中，Skills 实际可见可扩展，并修正宽屏布局。保留项目现有功能和绿色视觉风格，不整体改造为 DeerFlow，不提供宿主机 Python/Node 部署。

## 采用方案

- `compose.yaml` 默认构建后运行：镜像安装锁定生产依赖，API/worker不挂载源码；`compose.dev.yaml` 叠加源码、项目根Linux `.venv`、开发依赖和 Vite/API 热更新。两种模式使用相同数据库及工作目录，不能同时连接同一卷运行两套worker。
- 用户随后明确简化开发操作：开发模式只常驻dev/PG/Redis，API、worker、Vite均由dev内的`python scripts/dev.py`前台启动；不再把dev仅当编辑器容器、另起三个应用容器。初始化服务完成锁定依赖、Alembic迁移、旧正文迁移与视图同步后退出。`make dev`/`start.ps1 -Mode development`仅准备环境，日常启动/调试在dev终端完成。`make up`才启动独立部署服务并等待HTTP/消费者就绪。模式切换先停止旧应用及dev，不删除持久卷。
- Python入口使用标准库subprocess，不增加进程管理服务或Docker权限。API/worker仍是两个进程，经既有Celery/Redis通信；Vite在同容器代理localhost:8000，宿主8787映射到dev:5173。Ctrl+C、启动异常或子进程意外退出会回收进程组及普通后代，数据库保持运行。
- 不自动重启worker追踪源码变化。worker内有长任务、执行租约和恢复规则，开发者在dev终端停止并重新运行脚本；`make restart-worker`只适用于部署模式。API热更新不承担重新授权未完成任务。脚本不调用模型、不绕过现有预算，不改变.env模型开关。
- 根据用户进一步明确的可读性偏好，顶层改为`backend/`和`frontend/`，Python内部保留`backend/src/knowledgeops_agent`布局。根目录仍统一维护Python锁文件、开发`.venv`、数据库迁移及集成验证，避免复制依赖定义。构建、热更新、前端依赖卷、包安装及证据指纹同步使用新路径；容器服务名`web`和业务接口不变。根`.env`保存部署变量，`config/`保存结构化模型容量、搜索和规则；工具/MCP/角色管理状态继续在PG，不重复到环境变量。
- 从最新DeerFlow固定提交移植两份文本技能，并适配工具名、路径和权限；现有三份业务技能保留。部署目录→运行资源快照→Deep Agents SkillsMiddleware发现→read_file渐进加载的链条不变。技能目录只读挂载，不开放自修改或自动安装依赖。
- 页面外层占满侧栏右侧可用区域，再居中有界内容。保持对话阅读宽度，修正宽屏偏左；窄屏保留新对话、历史入口和模型状态，增加组合键发送与正确的加载/断连状态。

## 开源核对及区别

DeerFlow `main`在本轮固定为`c89fed0be4c64b4d881942a0fd240f239e239e96`。核对`Makefile`、`docker/docker-compose-dev.yaml`、`docker/dev-entrypoint.sh`、`docker/docker-compose.yaml`。它既有宿主机`make dev/start/install`，也有Docker开发/运行入口；其Docker开发是服务源码挂载和热更新，不等于必须使用VS Code Dev Containers。本项目只保留Docker两模式。用户随后选定的单开发容器多进程入口是本项目对开发体验的调整，不宣称照搬上游拓扑或脚本。

工具参考核对`backend/packages/harness/deerflow/config/tool_config.py`、`tools/tools.py`及`test_middleware_declared_tools.py`、`test_skill_tool_policy_middleware.py`。上游支持配置`name/group/use`动态加载工具，管理内置、中间件、插件、MCP等来源，还有技能关联的工具策略。本项目注册表绑定已实现的Python工具函数，分组及权限选择在PG；中间件工具也纳入目录并在模型请求前过滤。没有上游任意Python扩展导入、技能关联动态工具策略或全部沙盒/多媒体能力，不把相似名称描述为完全一致。

直接SDK复用仍是Deep Agents 0.7.23的Skills/文件/子Agent中间件。两份新Skill属于有MIT来源的正文移植；启动模式和工具分组属于设计适配，未复制DeerFlow部署脚本。具体来源及文本调整见根`skills/README.md`。

## 配置与兼容性

保留持久卷名称、数据库名、工具名、逻辑Skill路径及历史资源标识；不需要业务数据库迁移，不修改旧运行快照。技能和规则的物理文件搬到根目录，但旧运行仍通过快照恢复原文。配置错误应在发出模型请求前失败。

模型名称不再被`Settings`写死，仍须匹配显式模型容量文件和当前适配器。当前验证对象是DeepSeek Chat Completions；可配置不等于支持所有模型。固定256K/90%默认、根预算、发布权限、只读子角色和无公网认证边界保持。

后续配置审查修正：新增公开字段要同步本机.env缺项并保留用户值，不能只更新example。普通默认值公开、真实凭据留在忽略文件；APP_LOG_LEVEL统一进入API和worker。WORKSPACE_ROOT/SOURCE_STORAGE_ROOT是Compose与备份约定的内部路径，不作为被固定值覆盖的公开配置，用户通过AGENT_WORKSPACE_HOST选择宿主位置。升级后由当前模式入口重新注入环境，不为同步配置删除持久数据。

用户进一步确认：开发模式不能因为日常应用参数变化就反复重建容器。开发Compose使用官方`!reset`/`!override`清除继承的生产env_file及应用环境，只保留识别开发环境的标志，不注入数据库、模型、目录等应用配置；要求Compose至少2.24.4。dev.py使用已锁定的python-dotenv，每次启动读取当前根.env并生成数据库/队列连接、固定目录及子进程环境；--run入口共用于单独调试与测试；显式进程覆盖仍高于文件，不污染父终端，MODEL_CALLS_ENABLED=false的离线边界保留。旧dev首次需要重建容器以去掉遗留快照，启动器检查配置来源标志避免静默读旧值。此后模型、日志、超时、缓存、资源路径和轮询参数只需重启dev.py；不自动热重启worker。

生产保持创建容器时注入环境，普通配置变化只需重新创建API/worker，不重建镜像。端口、挂载仍属于容器生命周期，开发连接配置则在进程启动时生成，已有PG改密码还需修改数据库账户。开发和生产的本地执行后端均跟随worker进程，使用同一内部工作区路径及工具镜像；没有需要切换的沙盒HTTP地址，换任意宿主时仍必须核对挂载、依赖及权限。以上属于本项目部署适配，未声称摘取DeerFlow脚本；Docker环境及覆盖规则依据官方Compose文档。

本轮验证不调用付费模型。离线机制、容器构建/启动及浏览器证据见progress；不能把技能可读当作真实模型一定正确使用技能。
