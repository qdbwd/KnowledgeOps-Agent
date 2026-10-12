# KnowledgeOps

一个以聊天为主要交互、由 Agent 推进任务的 Web 应用：支持现有工具范围内的通用任务执行，重点特化知识库构建、维护与可追溯问答。

可以通过聊天解释问题、检索资料、分析内容、处理工作区文件或执行脚本并交付产物；这些任务不必转成知识文章。需要知识整理与维护时，Agent默认了解本应用的规则，并接入版本、引用和检索能力。知识库特化与通用工具组合共用同一条Agent执行循环；实际能力仍以当前工具配置为准。

启动入口：[首次准备](#首次准备) · [开发模式](#开发模式) · [生产部署模式](#生产部署模式) · [依赖与配置的位置](#依赖与配置的位置) · [修改后怎样生效](#修改后怎样生效) · [停止与启动排查](#停止与启动排查)。

想了解项目实际怎么实现，先读[完整开发实现记录](docs/architecture.md#当前实现的开发设计说明)：从 Agent 创建、状态、提示词与工具循环，接着解释知识业务、上下文管理，再到 Web 接单、会话延续、任务恢复和 UI；说明应用需求怎样反向改变 Agent，以及哪些组件直接复用、摘取或适配自参考项目。开发状态与验证证据集中在[进展](docs/progress.md)，不需要从测试流水中反推架构。

项目展示与面试准备的38题问答已迁入个人知识库的任务板块，见[迁移入口](docs/interview-qa.md)。完整正文在知识库维护，项目不再保留另一份问答副本；本仓库继续维护工程说明与验证证据。

2026-10-12增量增加两种学习模式、通用长期记忆和可维护的读者配置，数据库已从0018升级到0022。当前以[实施计划](docs/implementation-plan.md)和[决策0023](docs/decisions/0023-learning-modes-and-general-memory.md)为准，完成状态及验证证据见[进展](docs/progress.md)。本轮使用离线模型替身验证实际图、服务和浏览器，未进行付费语义质量验证；2026-10-09的23/24业务结果是历史基线，不能作为新学习模式的效果评分。

## 首版范围

- 对话、粘贴文本、Markdown/TXT、PDF/OCR 与公开网页材料。
- 联网获取证据、知识问答、正文附近的来源引用；围绕用户明确的学习与整理需要推进。
- 草稿、差异、受控发布、Markdown 编辑、版本/冲突、合并/拆分/归档/恢复和导出。
- 只读 Skills 渐进加载、独立输入的 `call_model`、可配置的只读子 Agent、完整请求容量检查。
- 同会话跨轮延续、FIFO 排队、用户选择运行中引导、自然语言授权、取消及手动恢复。
- 可管理的跨会话记忆、通用及任务学习配置；两种学习模式共用原会话历史，任务目标和进展独立保存。
- 通用 MCP 工具服务接入；默认没有已配置的外部服务。
- 固定业务评估、多轮交互、真实子 Agent 验证、备份恢复与可复现演示。辅助工具默认关闭，其效果实验已按用户要求移出本轮交付范围；各项当前证据见进展，能力存在不代表效果已验收。

## 两种学习模式怎么用

普通聊天仍是原来的入口，不必先创建学习任务。需要持续学习时，打开左侧“学习与整理”（`/learning`），选择已有会话或创建会话，填写目标、范围和初始知识单元；也可以在聊天里明确要求 Agent 创建或修改学习任务。推荐一个会话围绕一个主题，但同一会话允许切换任务，聊天历史不会被清空。

**交互学习**由 Agent 根据你的问题解释、提出框架和建议，再等待你的下一条消息。方向、详略和何时保存仍由你决定；具体问题不必重复经历框架开场，也不会在后台自行连续上课。

**委托整理**需要先保存并确认任务范围。允许自动保存时，主 Agent 起草与修订，读者子 Agent 找理解障碍，独立审读者检查完整草稿。程序验证审读对应的草稿版本和当前授权，再保存并推进下一单元。页面可查看真实子任务、阅读范围、问题和结果；审读次数耗尽或关键问题未解不会被标成通过。关闭浏览器不停止后台工作，暂停、取消和恢复使用任务页的明确操作。

进行中可以继续聊天：排队消息会在下一自动单元前优先处理，“引导当前任务”仍在安全点插入。修改已确认的任务要求会收回旧的自动推进许可；保存、确认新范围后再继续。普通配置编辑从下一次运行生效，即时纠正走引导机制。恢复不会清零知识单元已经消耗的预算。

从任务页进入“学习配置与读者角色”（`/learning-profiles`），可编辑通用或当前任务的人工 Markdown，预览并应用导入、比较版本、导出、停用或回退。自动部分来自有来源的长期记忆，不能静默覆盖人工正文。复用旧任务配置需要明确选择固定修订，只复制学习要求，不复制旧任务的目标、框架、授权、预算或执行进度。导入 Skill 风格文档只导入文字，不执行脚本或开放工具。

“长期记忆”（`/memories`）可查看背景、偏好、习惯、目标和纠正的来源、范围及修订，并确认、编辑、停用、合并或撤回。新真人消息通过既有语义解释入口产生候选，自动推进和子 Agent 的模拟提问不会被当成你的习惯。停用影响后续主动使用，不抹掉已经存在的历史聊天。

## 环境与边界

Python 3.12、FastAPI、LangChain/LangGraph、PostgreSQL 17 + pg_search BM25；Celery + Redis 后台任务；React/TypeScript/Vite 前端。运行部署模式用独立的 api、worker、web、postgres、redis 容器；开发模式由 dev 容器同时运行 API、worker、Vite 三个进程，PG/Redis 仍独立。init 完成依赖与迁移准备后退出。安装、运行与测试在本项目容器。

开发模式的 Python 环境实际位于项目根 .venv，对应容器 /workspace/.venv，解释器为 /workspace/.venv/bin/python。由初始化容器按根 uv.lock 安装，不使用 Windows/Conda Python；该目录不进入 Git。构建后运行模式的依赖位于镜像内。

知识工作文件、历史正文、上传原件及产物保存在专用宿主挂载目录 `data/agent-workspace`；PG保存版本身份、引用、关系、草稿、运行/聊天与派生BM25索引。正式知识可使用通用文件工具或脚本编辑，程序自动维护发布版本和索引，预览/冲突仍有明确回执。桌面个人知识库不作为运行时数据，不挂载、不复制、不回写。早期学习练习仅在本地保留，不随仓库发布，业务代码不依赖它。

模型配置由本地 .env 注入，密钥不进入 Git。配置与任务预算见[工程规则](AGENTS.md)。不接入旧 Agent-Evaluation-Framework，不做向量检索或GraphRAG。现已追加worker内本地命令执行，不创建AIO容器；它不是运行不可信代码的强安全沙盒。

## 本地文件与通用执行

默认将 Windows 的本项目 `data/agent-workspace` 挂载到 dev/API/worker 的 `/data/agent-workspace`。将材料放进 `work/`，就可以通过聊天让 Agent 找文件、阅读、搜索、精确编辑和执行 Linux 命令或 Python 脚本；普通交付放在 `outputs/` 并可下载。正式知识的编辑入口是 `knowledge/documents/`，支持多级目录，Markdown可以直接写入，不需要先生成Python脚本。完整目录分工见[工作区说明](data/agent-workspace/README.md)。

通用文件工具直接复用 Deep Agents 的接口；知识修改后的版本/引用/索引由同步中间件维护。同一文件的预览编辑修订原草稿，批准后才进入正式检索；同步失败保留文件并明确返回原因。Windows编辑器的外部修改不会后台自动发布，可让 Agent 用 `sync_knowledge_files` 指定同步。`knowledge/versions` 是不可变历史，`knowledge/current` 是阅读导出，两者不是编辑入口。

新工具沿用工具管理界面/API的开关，旧任务和子Agent不会因升级自动获得工作区。可通过 `.env` 的 `AGENT_WORKSPACE_HOST` 更换专用宿主目录；更换前先停止应用并搬齐整个工作区。备份必须包含业务数据库、模型回执库和完整工作区，不能只复制PG。命令可访问worker已有挂载，适用于本机可信任务；运行中约每秒检查取消、租约和活跃时间预算，默认60秒超时，`.env`的`WORKSPACE_EXECUTE_TIMEOUT`最多可设300秒。普通子进程在工具结束时回收，不支持跨调用后台服务；写出的文件保留。输出最多保留64,000原始字节，长日志请重定向到`work/`后分页读取。

## Docker 快速启动

项目只提供Docker环境。**宿主机负责创建/切换容器；开发时在dev容器里启动应用。** 下文的PowerShell命令在Windows项目根目录执行，`sh`命令在开发容器终端执行；不要求宿主机安装Python、Node、uv或npm。

| 模式 | 常驻容器 | 应用怎么启动 | 适用情况 |
| --- | --- | --- | --- |
| 开发 | dev、postgres、redis | 进入dev执行`python scripts/dev.py`，同时启动API、worker、Vite | 修改代码、调试、运行测试 |
| 生产部署 | api、worker、web、postgres、redis | 宿主启动入口构建镜像并自动启动各服务 | 使用构建好的应用，无源码热更新 |

两种模式共用业务代码和持久数据，一次使用一种。`compose.yaml`定义生产部署，开发入口额外加载`compose.dev.yaml`，用开发镜像和源码挂载准备dev，并停用独立的api/worker/web服务；生产入口不加载开发覆盖文件。两种模式的容器布局、依赖来源和热更新设置因此分别生效。`init`是一次性准备服务，成功后退出，不是常驻应用。生产部署模式仍为本机单用户部署，端口绑定127.0.0.1，不代表已经实现公网认证或多租户。

### 首次准备

宿主机需要已启动的Docker Desktop（Linux容器模式）或Docker Engine，以及Compose 2.24.4或更新版本（开发配置使用`!reset`/`!override`清除生产环境快照）。开发时推荐VS Code及Dev Containers扩展；Make可选，Windows直接使用下面的PowerShell入口即可。

克隆或下载仓库后，**在Windows PowerShell执行**（路径按实际位置调整）：

```powershell
Set-Location 'D:\codes\KnowledgeOps Agent'
# 仅首次创建；保留已有配置，避免覆盖密钥和个人设置。
if (-not (Test-Path -LiteralPath '.env')) {
    Copy-Item -LiteralPath '.env.example' -Destination '.env'
}
```

编辑项目根`.env`：

| 配置 | 用途与要求 |
| --- | --- |
| `POSTGRES_PASSWORD` | 首次初始化数据库必须设置。已有数据库改密码需要同步修改数据库账户，不能只改.env。 |
| `MODEL_CALLS_ENABLED` | 默认false，可启动界面和做离线检查，但不能执行需要真实模型的Agent任务；真实使用设为true。 |
| `OPENAI_BASE_URL`、`OPENAI_API_KEY` | 真实使用时填写模型渠道地址和密钥；当前适配与容量档案针对DeepSeek兼容路径。 |
| `AGENT_MODEL` | 默认deepseek-flash。更换模型/渠道时还需匹配`config/model-capacity.json`，不能只改名称。 |
| `KNOWLEDGEOPS_PORT` | 宿主网页端口，默认8787；两种模式都使用这个入口。 |
| `AGENT_WORKSPACE_HOST` | 专用文件工作区，默认`./data/agent-workspace`，不要指向桌面个人知识库。 |

其余可配置项及注释见[.env.example](.env.example)。真实`.env`不提交Git；首次无需自己创建`.venv`、安装前后端依赖或手工创建数据库表，下面的环境准备会完成这些工作。

`.env.example`是公开模板，其中端口、日志级别、超时和缓存容量等普通默认值可以进入Git；模型密钥、数据库密码保持空白，真实值只填写在忽略提交的`.env`。模板更新不会自动覆盖已有`.env`：更新项目时应补齐新增字段和注释，保留现有配置值，而不是再次整份复制模板。修改部署配置时，需要同时核对模板、本机配置、Compose注入和代码使用位置。

### 开发模式

**第一步：在Windows准备环境。** 首次启动、切换模式或更新依赖/容器配置时执行；普通应用参数的修改不需要重复此步：

```powershell
./scripts/start.ps1 -Mode development
# 已安装Make时，等价命令为 make dev，二选一即可。
```

入口先停止旧应用容器和dev，再构建开发工具镜像，由init安装锁定依赖并迁移数据库，最后启动dev。看到`Development environment ready`表示开发环境可用，**此时还没有启动应用，网页暂时打不开是正常的**。这一过程不会删除数据库卷或工作区。

**第二步：进入开发容器。** 在VS Code打开本项目根目录，执行`Dev Containers: Reopen in Container`，然后打开编辑器终端。没有VS Code时，也可在Windows PowerShell执行下面的命令进入同一个容器：

```powershell
docker compose exec dev bash
```

**第三步：在开发容器终端启动应用。**

```sh
cd /workspace
python scripts/dev.py
```

等待API显示启动完成、worker完成启动恢复检查并启动消费者，然后访问 **http://localhost:8787**。若修改过`KNOWLEDGEOPS_PORT`，使用修改后的端口。`dev.py`只管理当前容器中的API、worker和Vite进程，不执行Docker命令；日志直接显示在当前终端。按**Ctrl+C**停止应用，dev、PG和Redis继续保留。

**日常开发只需第三步。** dev和基础服务仍运行、依赖与容器配置没有变化时，不必重新执行宿主准备入口。`dev.py`每次启动都重新读取根`.env`，再启动三个进程；修改模型开关、日志、超时、缓存参数或资源路径后，Ctrl+C并重新运行即可，不重建镜像或容器。API和Vite支持源码热更新；worker代码修改也使用显式重启。Windows挂载默认轮询，原生Linux可通过`DEV_WATCH_POLLING=false`调整。保存`.env`不会自动重启worker；停止前先等待任务结束或在页面取消，重启后的未完成任务仍需用户明确恢复。

开发dev不注入应用配置，只保留识别开发环境的标志；Docker仍读取宿主端口和挂载位置建立容器。dev.py从当前文件生成PG连接、队列/测试连接及固定工作目录，将配置传给应用进程。显式进程环境仍可覆盖文件，例如`MODEL_CALLS_ENABLED=false python scripts/dev.py`临时启动离线应用，不改本机配置。不要在终端长期`export`旧应用参数，否则该覆盖会继续优先于文件。已有旧dev需要按新版Compose重新创建一次；启动器会识别并拒绝继续使用旧环境快照。

<details>
<summary>需要分别调试三个进程时</summary>

先停止统一启动脚本，再在dev内打开三个终端，分别执行；不能与统一启动同时运行。`--run`只加载同一套开发配置后执行给定命令，不启动另外两个进程；命令的退出码和终端信号保留。调试时可另外指定日志级别等CLI参数。

```sh
# 终端1：API。显式沿用Windows文件轮询配置。
cd /workspace
python scripts/dev.py --run python -m uvicorn knowledgeops_agent.api:app --host 0.0.0.0 --port 8000 --reload --reload-dir backend/src
```

```sh
# 终端2：Agent任务消费者。
cd /workspace
python scripts/dev.py --run python -m knowledgeops_agent.worker
```

```sh
# 终端3：前端。API代理地址由开发入口提供。
cd /workspace
python scripts/dev.py --run npm --prefix frontend run dev -- --port 5173 --strictPort
```

</details>

### 生产部署模式

完成[首次准备](#首次准备)后，**在Windows PowerShell执行**：

```powershell
./scripts/start.ps1 -Mode production
# 已安装Make时，等价命令为 make up，二选一即可。
```

入口构建后端应用镜像和前端静态镜像，运行init执行迁移，再启动独立的API、worker和web容器，等待HTTP与Celery消费者就绪。看到`Application ready`后打开 **http://localhost:8787**。不需要进入dev、不需要再执行`dev.py`或`npm run dev`。

API和worker使用镜像内安装的Python依赖；web使用Nginx提供构建后的静态文件。源码和宿主`.venv`不挂载到生产应用容器，修改源码需要重新构建；工作区、config和skills仍按Compose设置挂载。依赖安装和前端构建已经封装在镜像构建中，没有额外的宿主安装步骤。

### 依赖与配置的位置

```text
项目根目录/
├─ .env.example、.env           配置模板与本机部署配置
├─ pyproject.toml、uv.lock      Python依赖声明与锁文件
├─ .venv/                      开发用Linux Python环境
├─ backend/src/knowledgeops_agent/  API、worker和Agent源码
├─ frontend/
│  ├─ package.json、package-lock.json  前端依赖声明与锁文件
│  ├─ node_modules/            容器中挂载的前端依赖卷
│  └─ src/                     React界面
├─ scripts/                    dev.py及初始化、维护脚本
├─ tests/、migrations/         集成验证与数据库迁移
├─ config/、skills/            模型/搜索/规则配置与技能正文
├─ compose.yaml                生产部署配置
└─ compose.dev.yaml            开发模式覆盖配置
```

根目录是本项目的Python工程根，API、worker、维护脚本和测试共用同一套依赖；`backend`是源码目录，因此`.venv`与`pyproject.toml`、`uv.lock`一起保留在根目录。前端依赖由frontend中的npm清单单独管理，**不会装进Python虚拟环境**。

根`.env`既提供Compose部署参数，也提供后端运行参数。生产API/worker通过`env_file: [.env]`在创建容器时注入文件变量，再由显式`environment`覆盖内部连接、路径等少量值；init使用同一来源但强制关闭模型。web没有整份注入，PG只接收指定的数据库变量，Redis不接收模型配置。这是本项目Compose的声明，并非Docker看到`.env`就自动向所有容器注入。开发dev已清除该生产快照，应用参数由`dev.py`每次启动时加载。当前浏览器前端通过同源`/api`访问后端，无需维护包含模型密钥的`frontend/.env`；开发Vite代理到`127.0.0.1:8000`，生产Nginx代理到`api:8000`，业务通信代码复用。

后端的`Settings`使用Pydantic Settings读取、转换并校验配置，优先级为显式构造参数、进程环境、项目根`.env`、代码默认值。开发启动器先合并当前文件与显式进程覆盖，把相同有效配置交给三个子进程，不污染父终端；生产则读取容器创建时的环境。代码中的`45`秒等默认值是缺省回退；`MODEL_TIMEOUT_SECONDS`传给模型客户端，Redis开关/容量/TTL传给事件桥与检查点缓存，`APP_LOG_LEVEL`同时用于API和worker。配置与Skills资源加载器读取启动器或Compose传入的路径，再加载文件。端口、宿主目录和开发轮询由Compose或启动器/Vite读取，并非所有字段都属于`Settings`。

`WORKSPACE_ROOT`和`SOURCE_STORAGE_ROOT`是与挂载/备份配套的固定内部目录，生产由Compose传入、开发由dev.py设置，不作为`.env`可填写项。要更换Windows上的实际存放位置，配置`AGENT_WORKSPACE_HOST`；旧`KNOWLEDGE_BASE_ROOT`已不使用。开发入口按当前POSTGRES_PASSWORD和固定postgres/redis服务名生成连接，支持显式DSN供调试或隔离测试覆盖；生产仍由Compose生成连接。已有PG改密码须同时修改数据库账户，不能只改.env。应用进程读取文件不意味着Docker端口、挂载或PG账户也自动改变。

| 安装阶段 | 实际执行位置与内容 |
| --- | --- |
| 构建开发镜像 | Dockerfile安装Python、Node、uv和OCR等系统工具。 |
| 准备开发环境 | init在`/workspace`执行`uv sync --locked --group dev`，再进入frontend执行`npm ci`。随后执行Alembic和工作区迁移；失败时dev不会继续启动。 |
| 构建生产镜像 | 后端按uv.lock安装运行依赖（不含dev组）；前端按package-lock执行npm ci并构建静态文件。 |
| 启动生产应用 | init执行数据库与工作区迁移；成功后API/worker及web继续启动，不在运行时重复安装依赖。 |
| 每次运行dev.py | 直接使用已经安装的依赖，不安装、不迁移；只启动应用进程。 |

开发`.venv`实际保存在宿主项目目录，由Linux容器创建，解释器为`/workspace/.venv/bin/python`，不能当Windows虚拟环境使用。开发前端依赖使用Docker命名卷挂到`/workspace/frontend/node_modules`，所以Windows文件夹里不一定能看到依赖内容。生产依赖保存在镜像内，不依赖宿主机是否存在这些文件夹。

换设备后，根据锁文件重新准备环境，不复制旧`.venv`或node_modules。Git保存依赖清单、锁文件及`.env.example`，不保存实际依赖目录和真实`.env`。业务数据另按[备份、恢复与故障](#备份恢复与故障)迁移。

### 修改后怎样生效

| 改了什么 | 开发模式 | 生产部署模式 |
| --- | --- | --- |
| 前端源码、API源码 | 通常热更新；首次启动需先运行dev.py | 重新执行生产启动入口，重建镜像 |
| worker/Agent共享Python代码 | 停止并重新运行dev.py，让worker重新导入 | 重新执行生产启动入口，重建镜像 |
| .env中的模型、日志、超时、缓存、资源路径、轮询参数 | Ctrl+C后重新运行dev.py，读取当前文件；不自动打断任务 | 停写后重新创建API/worker，无需重新构建镜像 |
| 宿主端口、挂载、Compose配置 | 结束应用，在宿主重新创建受影响容器，再进入dev运行dev.py | 重新创建受影响的容器 |
| 已有PG的密码 | 修改PG账户并同步.env，然后重启dev.py | 修改PG账户并同步.env，重新创建应用容器 |
| 镜像或锁文件、数据库迁移 | 使用宿主开发准备入口，再进入dev运行dev.py | 使用宿主生产启动入口 |
| 只停止了应用进程，代码/依赖/配置都没变 | dev终端重新运行dev.py | 使用生产启动入口恢复服务 |

开发模式的普通应用配置在进程启动时读取，不依赖重新创建dev；它不是运行中的自动配置热更新。生产容器的环境是创建时快照，`docker compose restart`不能刷新它。只有普通应用配置变化、没有源码或迁移变化时，可在停止任务后执行`docker compose up -d --no-deps --no-build --force-recreate api worker`；镜像和数据库卷保留。旧dev首次切换到当前配置，可用`docker compose -f compose.yaml -f compose.dev.yaml --profile development up -d --no-deps --no-build --force-recreate dev`，编辑器随后重新连接；不需要重新安装依赖。常规模型开关使用`.env`，`-EnableModelCalls`只保留生产入口兼容，开发入口会提示改用文件或启动进程时覆盖。

需要**新增依赖**时，先停止开发应用，在dev内维护依赖清单与锁文件：Python在`/workspace`使用`uv add 包名`（开发工具使用`uv add --group dev 包名`）；前端在`/workspace/frontend`使用`npm install 包名`（开发工具加`-D`）。替换“包名”为实际依赖名，并将清单、锁文件一起纳入版本管理。不要只往某个容器里临时pip安装而遗漏依赖声明。

只需**按现有锁文件重新同步开发依赖**时，可在dev内停止应用后执行：

```sh
cd /workspace
uv sync --locked --group dev
cd /workspace/frontend
npm ci
```

这两条命令不执行数据库迁移。升级包含迁移或容器配置的版本时，使用宿主准备入口；不需要先手工同步一遍再重复初始化。更多部署资源的配置说明见[config](config/README.md)，技能扩展见[skills](skills/README.md)。

切换模式时，先结束当前任务和开发启动脚本，再在宿主运行目标模式的`start.ps1 -Mode development/production`或`make dev/up`。这些入口会先停止旧应用及dev，再重新准备所选模式，共用持久数据但不同时运行两套worker。不要只启动另一组容器并让原应用继续消费同一个队列。

### 停止与启动排查

以下命令均在**Windows PowerShell、项目根目录**执行：

```powershell
# 查看全部容器，包括已经完成的init；等价于 make status 查看运行状态。
docker compose --profile development ps --all
# 检查依赖安装或迁移失败。
docker compose logs --tail=100 init
# 生产应用日志；开发应用日志直接看dev.py所在终端。
docker compose logs -f --tail=100 api worker web
# 停止整组容器；等价于 make stop，保留数据卷与文件工作区。
docker compose --profile development stop
```

| 现象 | 先检查什么 |
| --- | --- |
| dev容器运行，但8787打不开 | 是否在dev终端启动了dev.py；dev容器运行不等于应用进程已启动。 |
| init失败、依赖没安装好 | 查看init日志；锁文件不匹配时修正依赖声明/锁文件，不跳过失败继续启动。 |
| 页面出现代理错误 | API是否已完成启动，是否与Vite在同一dev内；首次冷启动需要等待Python模块加载。 |
| 提示启动脚本已运行或8000/5173被占用 | 在原终端停止旧应用，不要同时使用统一入口和手动启动的进程。 |
| 页面显示模型调用关闭 | 检查.env的MODEL_CALLS_ENABLED；开发重启dev.py，生产重新创建API/worker；再检查密钥/渠道配置。 |
| 修改代码后仍是旧行为 | 确认当前模式；worker需重启，生产应用需重新构建，不能依赖源码热更新。 |

init正常退出是预期行为。停止应用、停止容器和删除数据是不同操作；日常更新或重启不需要删除持久卷。数据库和完整工作区应一起备份，详见下文维护说明。

## 联网搜索配置

编辑[config/web-search.yaml](config/web-search.yaml)，可设置DDGS后端、地区、SafeSearch、默认结果数量和SDK超时，不需要修改Python工具函数。默认沿用Brave/DuckDuckGo/Startpage/Yahoo、中文查询自动选cn-zh、其他选wt-wt、5条结果和15秒超时；每次结果仍限1至8条。`backend`支持列表或逗号字符串，可用名称及验证规则见配置模块；`auto/all`是交给DDGS选择，不能保证更好的召回。

Agent的`search_web`新增可选`time_range`（day/week/month/year），仅在需要近期信息时使用，默认不限日期。时间筛选只能表达搜索意图，阅读正文时仍需核对发布日期。工具开关继续由“工具管理”控制，编辑搜索参数不会获得额外业务权限。

配置在新根任务首次执行时读取并保存到运行资源快照的`web_search`字段；子任务和手动恢复沿用同一快照。部署本次代码需要重启API/worker；此后单纯修改YAML不需要重启，下一新任务生效。旧运行缺少该字段时保留原默认值，不用当前配置伪造旧历史。非法配置会在首次模型调用前报错。没有新增MCP服务、API Key或检索模型。

### 会话与知识正文的BM25检索

Compose使用ParadeDB官方`0.26.1-pg17`镜像并固定摘要，原PG数据卷继续挂载；正常迁移由启动脚本的`alembic upgrade head`启用扩展并回填片段。不能换回不含扩展的普通`postgres`镜像后期待检索仍能工作。扩展附带vector依赖，本项目没有使用向量搜索或收费embedding。

历史和知识使用独立索引与权限，但共用中文分词、递归分段和BM25引擎。搜索返回稳定ID/版本、命中摘录和原文偏移，读取工具据此补读；正文无需出现在聊天中即可检索。分段初值1200字符/160重叠；这仍是词法检索，不保证任意同义表达命中。当前设计与开源参考见[架构说明](docs/architecture.md#从找资料到得到可引用证据)及[决策0020](docs/decisions/0020-pg-search-bm25-and-prompt-assembly.md)。必要时显式重建派生索引，不改原文或模型回执库。先按开发模式准备好dev，再在Windows项目根目录执行：

```powershell
docker compose exec -T dev python scripts/dev.py --run python scripts/reindex_knowledge.py
docker compose exec -T dev python scripts/dev.py --run python scripts/reindex_history.py
```

从旧bookworm镜像切换到当前trixie镜像时，除PG主版本外还要检查排序规则版本；本机已完成受影响索引重建再刷新版本。备份恢复目标也需使用兼容的pg_search镜像，不能仅凭持久卷存在就跳过版本兼容检查。

搜索可能返回空结果、超时或不相关页面；模型可调整查询后再搜。搜索摘录不等于实际阅读，引用仍需经过`read_web`/`read_source`。本轮配置与真实网络检查、开源复用出处见[开发说明](docs/architecture.md)及[进展](docs/progress.md)。

Redis现在有三项用途：Celery任务投递（DB 0），SSE实时事件桥，以及检查点复杂内容缓存（后两者使用DB 1）。正式事件、检查点、聊天和知识版本元数据仍在PG，知识正文现已文件化；缓存不可用时观察/读取退回PG，整个Redis服务不可用仍会影响Celery投递。`REDIS_EVENTS_ENABLED`与`REDIS_CHECKPOINT_CACHE_ENABLED`可分别关闭，修改`.env`后按[修改后怎样生效](#修改后怎样生效)重启开发进程或重新创建生产应用容器。不要在仍有运行任务时重启worker；服务重启后的任务仍按原规则等待手动恢复。

缓存默认值见`.env.example`，原理与限制见[0021](docs/decisions/0021-redis-streams-and-checkpoint-cache.md)和[开发实现记录](docs/architecture.md)。缓存不需要备份；既有恢复脚本创建独立数据库，因此使用独立键空间。人工原地覆盖同名数据库时，需要先停写并清除其旧缓存命名空间。

## 五分钟业务演示

先按开发模式准备好dev，在Windows项目根目录执行以下命令导入自包含合成材料。导入步骤不需要个人知识库，也不调用模型；后续聊天演示需要真实模型：

```powershell
docker compose exec -T dev python scripts/dev.py --run python scripts/seed_demo.py
```

1. 在“对话”中新建会话，选中“任务预算”和“恢复与幂等”材料，选择“先看草稿”。发送：`请把这两份演示材料整理成一篇连贯的知识文章，说明预算、恢复与幂等边界，引用已读材料，先给我审阅。`
2. 进入变更集查看完整正文、差异和影响；确认发布后，在“知识”中查看文章和历史。
3. 返回同一会话，选择“仅知识库”，发送：`主 Agent 使用12次、研究者8次，还剩多少次？重启后能自动继续吗？引用刚才的知识版本。`
4. 打开回答中的引用，查看固定版本和片段。编辑知识后再查看旧回答，旧引用仍指向旧版本。
5. 可测试拒绝、归档/历史恢复、刷新页面找回进度。合并/拆分先展示影响；明确选择“直接保存”可自动完成普通新增/修订，仍不能跳过合并/拆分确认。

真实演示需要在`.env`启用`MODEL_CALLS_ENABLED=true`，开发重启`dev.py`、生产重新创建API/worker以读取配置。每个任务保留自身预算、输入输出回执和恢复关联；应用不维护项目累计用量。完整场景与开发用例见[evaluation/cases.json](evaluation/cases.json)，开发报告不作为用户功能展示。

项目累计统计已按用户要求停用，任务预算及执行回执保留，见[预算修订](docs/decisions/0018-cumulative-budget-2000.md)。模型是否启用以当前环境配置为准。辅助工具默认关闭，效果实验已按[决策0019](docs/decisions/0019-remove-auxiliary-experiment-gate.md)取消。当前数据库0022，意图解释入口同时处理真人控制、通用记忆及学习配置候选；上下文仍按256K/90%处理，任务背景、历史补读和来源笔记保留。旧I04等结果有其当时的版本和语义边界，不作为本轮学习模式的效果验收。

仍有内容质量限制：K09回答把创建虚拟环境与安装依赖的先后顺序说反；真实聊天中保留了四/五计数、个别措辞和一个加粗排版问题；子Agent机制通过不表示子答复一定正确。旧批次分数与失败原文保留在[进展](docs/progress.md)和[开发问题](docs/development-issues.md)，交付不等于模型输出没有错误。

## 验证与评估

测试PDF不随仓库上传。生成代码保留在`evaluation/fixtures.py`，正式评估入口会在材料缺失时自动生成；PDF解析测试也会自行构造材料。在开发容器的`/workspace`下，也可单独准备合成PDF，不调用模型或执行评估：

```bash
uv run python -c "from pathlib import Path; from evaluation.fixtures import make_pdf_files; make_pdf_files(Path('evaluation/materials'))"
```

以下为开发者检查入口，不是每次启动的必做步骤。先准备好开发环境，再在Windows项目根目录按改动范围选择执行；命令将检查交给dev容器：

```powershell
docker compose exec -T dev ruff check backend/src tests evaluation scripts
docker compose exec -T dev mypy backend/src
docker compose exec -T -e MODEL_CALLS_ENABLED=false dev python scripts/dev.py --run pytest -q
docker compose exec -T dev sh -c 'cd frontend && npm run lint && npm run build && npm test'
docker compose exec -T -e MODEL_CALLS_ENABLED=false dev python scripts/dev.py --run sh -c 'cd frontend && npx playwright install chromium && npm run test:browser'
```

测试使用 `knowledgeops_test` 的独立 schema、Redis 的测试数据库和模型替身，不消耗真实模型额度。浏览器流程使用真实 HTTP/数据库，供应商仍是离线替身。按用户决定不使用 GitHub CI；这些检查在本项目开发容器中按改动范围执行。

以下是**收费评估**入口，仅在明确授权及任务预算内运行；每次新建不可变批次，失败不会被覆盖。执行前停止开发应用进程，以免验证进程和 Celery 消费者争抢同一执行租约，保持dev和基础服务运行；下列命令仍在Windows项目根目录执行。业务数据使用隔离 schema，调用回执与每根任务预算仍由统一网关保存，不恢复已废弃的项目累计统计。旧 comparison 的36次开关对照保留历史，本轮不要求重跑：

```powershell
docker compose exec -T dev python scripts/dev.py --run python -m evaluation.run --suite business --online
docker compose exec -T dev python scripts/dev.py --run python -m evaluation.optimization --suite interactive --online
docker compose exec -T dev python scripts/dev.py --run python -m evaluation.optimization --suite delegation --online
```

报告保存在业务数据库及`artifacts/evaluations/<report_id>/`，供开发者从文件和开发工具审查原始回答、草稿、引用、机械检查与审读意见；不提供用户评估页面或产品HTTP报告接口。执行完成仍需审读，裁判高分不会自动成为通过；报告完成不等于项目验收完成。失败回归用 `--suite regression --cases Kxx --parent-report <id>` 关联原批次。

`auxiliary`执行器仅保留历史复现用途，本轮不需要运行；重新开展辅助模型效果实验需用户另行提出，不因它出现在代码中而默认启用。

`--judge` 是可选的辅助模型评分，会额外消耗每例一次请求；省略它仍需相同的程序检查与逐例正文审读。当前渠道的辅助裁判可能耗尽输出长度而没有可解析评分，失败与未知用量如实记录，不把空评分当通过。项目开发代理的审读也不能冒充用户亲自验收。

## 优化功能的使用方式

开始新主题时，说明想了解的方向即可。核心知识库规则由应用自动装配；Agent 可按需读取“交互学习”“知识整理”“证据检查”，以及从DeerFlow移植的“代码文档”“论文审读”五项技能。文件及扩充方式见[Skills目录](skills/README.md)，能力页可查看发现的名称、用途与版本。之后可以直接追问、纠正、要求草稿或修改上一版，不必重复说明知识库用途。技能是方法指导，不能保证任意主题都覆盖完整，文档是否合用仍由用户判断。

普通消息在当前任务运行时默认排队。排队项的“引导当前任务”用于改变正在执行的根任务，成功投递后该消息不再单独执行；“取消排队”只取消对应排队任务。撤销写入会阻止后续发布，已提交的事务和已发送的模型请求仍保留真实结果与费用。关闭网页不会取消后台任务。

“先给草稿”“把刚刚修订的这一版保存”“不要保存”等明确表达会由意图解释器转换为受校验的业务操作。草稿修订号、会话、基础版本及影响范围仍由程序检查；合并、拆分、批量归档需要明确确认实际影响。也可在“变更与运行”中使用相同业务入口操作。

“长期偏好”页面可查看、修改、确认、停用、删除及查看修订。明确长期适用的真人表达可自动提取；临时要求不会持久化，推测和冲突需确认。撤回后不再注入后续相关模型调用；历史快照仍保留，不等同于擦除过去记录。

“工具服务”页面支持 stdio 或 Streamable HTTP 的 MCP 配置、连接测试、工具发现、权限与启停。初始列表为空，服务密钥只填写环境变量名。stdio 入口必须已安装，应用不自动安装或执行 shell 拼接命令。新工具默认禁止；用户可审查后允许只读使用，可能产生外部修改的调用逐次展示参数等待确认。结果未知时不会自动重试，MCP 的模型 sampling 被禁用以避免绕过计费网关。

“工具与能力”页面（`/tools`）提供本地及中间件工具的全局开关、主Agent工具组/单独选择、修订历史，并保留原MCP和子Agent管理入口。主Agent默认继承；取消继承且不选任何组/工具表示显式不选。全局关闭同时约束主子Agent，主角色的选择仅限制自身。修改冲突会保留本地输入，核对后可明确放弃并刷新。

页面下方的有效预览读取已保存配置，可以切换角色与任务选项查看不可用原因；它不创建任务、不连接MCP或调用模型，也不包括上方未保存的编辑。关闭read_file会关闭Skills按需读取，但核心知识库规则仍生效。普通扩充供新任务使用，关闭会在后续调用前检查；重新启用不恢复旧任务已撤回的许可。已经执行的操作不会被开关撤销。

运行详情可查看冻结的规则、技能、偏好、子Agent和 MCP 版本、延续关系、调用回执、摘要与长结果产物。模型接收组织后的任务背景与工作历史；发生压缩后采用摘要和近期完整消息组，缺少原文时可检索补读，原始记录保留供核对。没有每轮自动语义筛选全部历史；跨轮背景不继承旧任务的授权、预算或待执行动作。`call_model` 使用主 Agent 提供的独立提示词和获准材料，不继承主历史，也没有业务工具权限。

## 不花模型费用的开发演示

下面的浏览器场景在本项目dev内建立隔离测试数据，用确定性模型替身驱动真实主/子 Agent 图、文件、数据库和 UI。它覆盖两个知识单元、真人纠正、人工配置更新、独立审读、暂停恢复和正式保存，也检查普通聊天及交互学习。不是向正常应用写入演示会话，不需要开启真实模型。准备好开发环境后，在Windows项目根目录执行：

```powershell
docker compose exec -T -e MODEL_CALLS_ENABLED=false dev python scripts/dev.py --run npm --prefix frontend run test:browser -- tests/learning.spec.ts tests/learning-configuration.spec.ts
```

浏览器首次使用需安装锁定Playwright对应的Chromium，可在dev的`/workspace/frontend`执行`npx playwright install chromium`。这只用于浏览器测试，正常使用Web界面不需要。测试专用执行接口只存在于 `tests/browser_server.py`，正常服务不提供。真实运行中的意图解释、记忆提取、读者和审读仍使用模型并产生费用；离线通过只证明工程流程，不证明真实模型的框架、讲解和审读质量。

## 备份、恢复与故障

正常备份前先让任务结束，并停止应用写入和本地工作区编辑，保留postgres运行。开发模式在dev.py终端按Ctrl+C；生产部署模式在Windows执行`docker compose stop api worker`。然后在Windows项目根目录创建备份：

```powershell
./scripts/backup.ps1 -Mode create
```

备份脚本使用临时应用容器打包文件，不依赖dev中的应用进程；如果从未构建过生产应用镜像，可先在Windows执行`docker compose build api`准备该镜像，不必切换部署模式。备份完成后，开发模式回到dev重新执行`python scripts/dev.py`；生产部署模式执行`./scripts/start.ps1 -Mode production`。

需要验证恢复时，另行在Windows执行以下命令，传入上一条命令输出的实际备份路径。这会创建隔离数据库和文件卷，不恢复到正在使用的数据位置：

```powershell
./scripts/backup.ps1 -Mode restore -BackupFolder '<本项目 artifacts/backups 下的备份目录>'
```

备份格式2包含PostgreSQL业务库、独立模型回执库副本、完整工作区包及SHA-256清单。恢复脚本验证哈希，在新数据库/卷验证原件、正文文件、版本、引用、索引与检查点，不切换当前应用、不回滚当前模型回执库。旧格式1的原件卷备份仍可读取，恢复验证会先升级隔离库表结构。跨设备应将PG与完整工作区作为一组恢复，再显式调整连接/挂载；模型回执副本用于追溯和恢复核验，不用于覆盖当前调用记录。此脚本的自动验证目标是本机隔离恢复，不是一键切换到恢复后的数据。

- 任务服务重启：遗留任务转“中断”，页面选择恢复；关闭浏览器本身不会停止任务。
- 取消/预算耗尽：保留已发布版本和草稿，展示部分结果；恢复后的新模型请求仍计费。
- 供应商/搜索暂时失败：显示真实停止原因，不承诺自动解决外部可用性；需要时明确恢复或另开任务。
- 旧 SDK 丢失协议字段的历史检查点：无法凭空补回，保留原运行证据后从会话发起新任务。
- OCR 适用于普通中英文扫描页；复杂图表、公式、手写和识别歧义需核对原件。
- 引用校验拒绝无效或越权证据ID；带已验证真实ID的引用标记会转换为可点击链接，缺少ID的占位文字会被拒绝或要求修正。校验仍不能证明每条陈述都被来源支持。原评估中两篇失败文章与报告保留，不自动改写成成功。

## 代码阅读地图

`api.py` 负责 HTTP 交互，`worker.py`/`task_queue.py` 负责 Celery 投递与执行；`runtime.py` 组装 Agent 和工具。`run_resources.py`/`skill_resources.py` 冻结规则与技能，`publication_intent.py` 解释保存选择与偏好，`preferences.py` 管理偏好修订。`continuation.py`/`guidance.py` 区分跨轮背景与运行中引导；`context_management.py`/`request_capacity.py` 组织并检查完整请求。`auxiliary_model.py` 实现独立模型工具；`mcp_registry.py`/`mcp_transport.py`/`mcp_calls.py` 分别负责 MCP 配置、协议与调用许可。`model_gateway.py` 统一计费与记录，`deepseek_client.py` 适配供应商协议。`knowledge.py` 保证版本、权限、引用和事务；`sources.py`/`pdf_sources.py`/`web_research.py` 管理材料；`evaluation/` 保存固定场景和执行器；`frontend/src/` 为界面。

Redis承担Celery broker、实时事件Streams和确定检查点内容缓存；PostgreSQL仍是任务、事件、正式检查点和业务结果的依据。消息重复、事件缺口、进程中断与业务幂等分别处理。选型依据见[ADR 0008](docs/decisions/0008-celery-redis-task-queue.md)和[ADR 0021](docs/decisions/0021-redis-streams-and-checkpoint-cache.md)。

## 维护入口

- [项目上下文](PROJECT_CONTEXT.md)：改动前读取。
- [架构](docs/architecture.md)：模块、存储、权限与接口。
- [个人知识库面试问答的迁移入口](docs/interview-qa.md)：完整问答位于知识库任务板块。
- [实施计划](docs/implementation-plan.md)：唯一持续执行计划与验收。
- [进展](docs/progress.md)、[开发问题与解决](docs/development-issues.md)、[设计决定](docs/decisions/)。
- [规则参考](docs/references/personal-knowledge-base/README.md)：资料，不作为开发指令。

计划导出保留为历史快照，后续维护正式文档。

### 子Agent配置与辅助模型

“子Agent”页面可创建角色，填写名称、用途和专属指令，明确选择可用工具组、单独工具与Skills后启用。组与单独工具合并后保存固定结果，只改提示不会扩大成员；“查看有效工具”转到统一预览。内置researcher保留为一般只读角色；新角色默认停用。聊天中的“允许子Agent分工”表示允许主Agent按任务需要选择角色，不要求每次委派。可直接说明“把两份材料分别交给两个子Agent核对，再综合结论”。

角色继承同一模型与根预算，每次最多两个活跃子任务，不递归、不发布知识，不新增脚本或MCP权限。保存角色配置不调用模型。配置名称创建后不可改，删除保留修订历史；冲突时刷新核对，不能覆盖他人已保存的新修订。普通编辑与新增许可供新根任务使用；禁用、删除或收窄工具/Skills使旧绑定在下一调用前失效，再启用也不会复活旧绑定。

实现直接使用Deep Agents的SubAgentMiddleware、CompiledSubAgent和task工具；本项目负责PG目录及现有运行服务的连接，不复制其循环源码。最新上游参考、版本和适配理由见[决策0016](docs/decisions/0016-configurable-subagent-catalog.md)。

“允许辅助模型”默认不勾选。需要独立提示处理材料时可显式开启call_model；它是可选工具，不代表必然改善结果，也不等于子Agent委派。两种调用都计入相同模型回执库。
