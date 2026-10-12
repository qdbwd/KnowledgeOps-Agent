# Agent Skills

这是应用内 Agent 的技能目录，不是 Codex 开发助手的 `.agents/skills`。每个直接子目录放一个 `SKILL.md`，根目录的说明和许可不进入模型。

| 技能 | 用途 | 来源 |
|---|---|---|
| guided-learning | 梳理学习框架、交互追问、纠正误解 | 按本项目已确认的产品行为编写 |
| knowledge-maintenance | 把已明确的材料整理为知识正文并遵守发布流程 | 本项目业务规则 |
| evidence-check | 阅读原文、核对引用与结论 | 本项目业务规则 |
| code-documentation | 阅读代码后生成 README、接口和架构说明 | DeerFlow 正文移植、工具及路径适配 |
| academic-paper-review | 阅读论文、核对论据、给出有依据的审读 | DeerFlow 正文移植、工具及路径适配 |

启动后主 Agent 先看到名称和用途；需要某项方法时通过 `read_file` 读取 `/skills/<name>/SKILL.md`。不会把五份正文每轮全部塞入提示词。`/tools` 可查看发现的技能；子 Agent 配置可选择技能子集。关闭 `read_file` 会阻止技能读取，但核心知识库规则仍独立生效。

新增技能：在这里建立目录，填写 YAML frontmatter 的 `name`（等于目录名）和 `description`，正文写触发条件、步骤、工具用法及限制。`metadata.version` 可选；没有时用正文哈希作为版本。允许附属 Markdown 参考文档；当前冻结加载器不打包脚本、二进制或自动安装依赖。需要脚本的上游技能必须先适配执行与资源路径，不能只复制说明就称可用。

新根任务读取当前文件、保存完整文本与哈希；旧任务恢复保持原快照。更换整个目录可设置 `.env` 中 `SKILLS_HOST_DIRECTORY`，容器仍在 `/workspace/skills` 读取。删除文件只影响新任务，不等同于撤销正在运行任务的历史权限。

两份移植技能来自 [DeerFlow c89fed0](https://github.com/bytedance/deer-flow/tree/c89fed0be4c64b4d881942a0fd240f239e239e96/skills/public)，许可保存在 `LICENSE-DeerFlow`。保留了主体方法和输出结构，新增明确的 KnowledgeOps 适配段；将 `web_fetch` 对应到 `read_web`，交付路径改成工作区 `/outputs/`，修正代码阅读示例，去掉依赖未安装 `deep-research` 的要求，论文评价不强行凑优缺点数量。不引入上游的生图、音视频、外部部署或额外收费服务。上游文件不是本应用权限来源。
