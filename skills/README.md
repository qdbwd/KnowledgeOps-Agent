# Agent Skills

这里维护应用内 Agent 的技能包，不是 Codex 开发助手的 `.agents/skills`。每个直接子目录的 `SKILL.md` 是唯一入口；`scripts/`、`references/`、`assets/` 属于该包。内部测试样例即使也叫 `SKILL.md`，也不会成为另一个技能。根 README、许可和启用配置不进入模型。

## 当前可用范围

2026-10-13检查用户复制的DeerFlow技能后，共保留26个包，启用12个。启用表示已适配当前接口和路径，不代表验证过模型完成任何同类任务的质量；工具关闭时仍不能执行相应操作。

| 技能 | 当前处理 |
| --- | --- |
| guided-learning、knowledge-maintenance、evidence-check | 原有学习、知识维护和证据方法，保留 |
| code-documentation、academic-paper-review | 前轮适配的DeerFlow代码文档、论文审读技能，保留 |
| consulting-analysis | 分析框架/报告方法；尊重任务范围与篇幅，无图表工具时用表格 |
| deep-research | 多来源研究；改用search_web/read_web，不再要求每个简单问题都多轮搜索 |
| frontend-design | 界面设计方法，保留原许可；去掉强制DeerFlow标记，尊重已有风格与入口 |
| github-deep-research | 公开GitHub研究；脚本、报告模板随快照加载，通过execute运行，匿名API限额/网络错误如实返回 |
| newsletter-generation | 资讯简报，适配来源读取与/outputs交付；生成文件不意味着发送、发布或订阅 |
| surprise-me | 组合已开放技能，只读取选中方法，不因“惊喜”安装依赖、启用服务或发布 |
| web-design-guidelines | 用read_web获取指南，结合当前可读文件审查；联网关闭时说明限制 |

其余14个包不满足当前运行条件，原因由[availability.json](availability.json)统一配置，并在Web“工具与能力”中展示为不可用。用户已要求删除这些包；本轮目录删除被开发工具自动审批以“blocked by policy”拦截，文件尚未删除，以下条目继续阻止它们进入Agent：

| 技能 | 尚缺的适配 |
| --- | --- |
| bootstrap | DeerFlow的SOUL/USER初始化，不对应本应用已有学习配置与长期记忆入口 |
| skill-reviewer | 缺少review_skill_package；evals内是校准样例，不能注册为业务技能 |
| skill-creator、find-skills | DeerFlow专属技能管理/安装接口和目录约定 |
| claude-to-deerflow | 需要另一个运行中的DeerFlow应用 |
| data-analysis | DuckDB/openpyxl等依赖未声明，原脚本还会自行pip安装 |
| systematic-literature-review | 文献流程、路径与检索脚本尚未整体适配；脚本部分可用标准库运行 |
| chart-visualization | 会向外部图表服务传输数据，尚未核对该服务和输出合同 |
| ppt-generation | 幻灯片脚本、资源路径、可选生图和运行依赖尚未整体适配 |
| image-generation、music-generation、podcast-generation、video-generation | 额外媒体服务、凭据或依赖，部分涉及费用，没有自动接入 |
| vercel-deploy | 尚未配置对外部署授权和产物传输流程 |

纯文本也可能引用不存在的工具、改变用户偏好的工作方式，不能只按扩展名判断兼容性。以上是兼容性检查，不是全部第三方代码的安全审计。

## 发现、读取和执行

Deep Agents SkillsMiddleware先提供启用技能的名称与用途；模型决定使用后，通过read_file读取`/skills/<name>/SKILL.md`，再按需阅读脚本、参考资料或模板。不会把所有正文和代码预先拼入提示词。子Agent只能看到所选技能，当前仍没有工作区shell。

根任务首次执行冻结正文、脚本和资源字节及哈希，非UTF-8资源以Base64保存在同一JSON快照。修改部署文件影响新根任务，旧任务继续原内容。每文件最多1MiB，整体最多8MiB/2048文件；超限报错，不静默漏掉依赖。排除隐藏项、Python缓存及node_modules，拒绝符号链接和越界路径。

脚本不自动导入、执行或安装依赖，不必逐个注册为模型工具。主Agent取得现有execute权限后，可以运行：

```sh
python "$SKILLS_DIR/github-deep-research/scripts/github_api.py" langchain-ai deepagents info
```

SKILLS_DIR指向本次命令临时生成的固定包目录，包括同版本的脚本和相对资源，调用结束清理。文件工具仍用`/skills/...`虚拟路径；命令工作目录仍是挂载区，输出写`outputs/...`。不保存临时地址供下次调用。没有execute权限或仅知识库模式时不能运行脚本；命令继续走超时、取消、预算与输出管理，不继承应用密钥。

准备资源记为skill_materialized，表示提供了哪些固定资源，不冒充模型逐行阅读。显式阅读仍记为skill_read。入口撤回阻断附属脚本；已使用资源撤回后停止后续调用，历史不会被“擦除”。本地shell仍有所在容器的权限，目录只读标记不构成抵抗恶意代码的强隔离。

## 新增与启用

Web“工具与能力 → 已发现的任务技能”提供用户开关，与工具选择一同保存配置。availability.json是部署兼容性上限，PG中的ToolPolicy.skill_enabled保存用户选择，最终必须两者都允许；界面不能强行开启缺少运行条件的包。配置历史仍在同一页面查看。关闭会撤回旧运行读取/执行资源的能力，重新开启只供新运行使用；已读取内容不从历史中抹除。子Agent的持久角色可保留已关闭技能名，实际运行只装配根运行获准的技能。

新增`skills/<name>/SKILL.md`，填写与目录一致的name、非空description，正文说明触发范围、方法、所需工具、脚本参数与依赖。metadata.version可选，没有时使用入口正文哈希作显示版本，附属文件另有哈希。新增包默认启用；暂不能运行的包在availability.json的disabled中写明原因。适配验证后移除对应条目，用于新任务，无需修改工具注册代码。

更换整个目录用.env的SKILLS_HOST_DIRECTORY，容器路径由部署提供。包使用已安装的Python/Bash/Node等环境；支持带脚本的技能不意味着其依赖都已安装。没有自动凭据绑定或Skill自我安装。

Skill里的allowed-tools不是本应用的授权来源，目前也不按它另建一套运行权限。真正可用工具仍由统一工具策略、根任务限制与角色许可计算；不要靠复制一个声明扩大权限。

## 来源与采用方式

前轮两份移植方法基于[DeerFlow c89fed0](https://github.com/bytedance/deer-flow/tree/c89fed0be4c64b4d881942a0fd240f239e239e96/skills/public)。本轮检查和适配用户复制的包，核对上游最新提交为[3343522](https://github.com/bytedance/deer-flow/tree/3343522fa2e62b1a1bb7df430affcba647103daf)。保留LICENSE-DeerFlow与frontend-design/LICENSE.txt；改动正文注明KnowledgeOps适配。

运行侧直接使用Deep Agents 0.7.23。包边界参考DeerFlow的LocalSkillStorage及test_load_skills_stops_at_skill_package_boundary；启用资源投影参考skills/projection.py，但本应用投影固定运行快照。没有安装整套deerflow-harness，其广泛的模型、沙盒、存储与运行设施不是此次修复所必需。
