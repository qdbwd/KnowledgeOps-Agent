# Agent Skills

这里维护应用内 Agent 的技能包，不是 Codex 开发助手的 `.agents/skills`。每个直接子目录的 `SKILL.md` 是唯一入口；`scripts/`、`references/`、`assets/` 属于该包。内部测试样例即使也叫 `SKILL.md`，也不会成为另一个技能。根 README、许可和启用配置不进入模型。

## 当前可用范围

2026-10-19按用户要求删除14个不兼容的DeerFlow技能包，当前目录保留12个已适配包。部署可用表示已适配当前接口和路径，不代表验证过模型完成任何同类任务的质量；用户开关或工具权限关闭时仍不能执行相应操作。

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

已删除的包为bootstrap、chart-visualization、claude-to-deerflow、data-analysis、find-skills、image-generation、music-generation、podcast-generation、ppt-generation、skill-creator、skill-reviewer、systematic-literature-review、vercel-deploy、video-generation，包括它们的脚本、模板和测试样例。它们此前缺少本应用接口、完整运行依赖或外部服务，不能通过复制说明文件自动补齐；没有为保留这些包安装依赖或接入服务。

[availability.json](availability.json)继续作为部署禁用配置入口，当前disabled为空；被删除的包不再出现在技能目录中。保留此机制方便维护实际存在但需要暂时停用的包，不再以“禁用”代替用户要求的删除。中文展示映射和其他技能中点名调用已删除包的示例也已清理。

本地仍有这14个包的空目录：逐文件补丁已删除全部92个文件，但随后只删除空目录的命令仍被开发工具自动审批拒绝（仅返回blocked by policy）。空目录没有SKILL.md，不会被加载器发现，也不属于Git跟踪内容；此限制不应被写成技能文件仍在。

纯文本也可能引用不存在的工具、改变用户偏好的工作方式，不能只按扩展名判断兼容性。格式检查和已知禁用原因不等于所有脚本依赖都已验证，也不是全部第三方代码的安全审计。

## 发现、读取和执行

Deep Agents SkillsMiddleware先提供启用技能的名称与用途；模型决定使用后，通过read_file读取`/skills/<name>/SKILL.md`，再按需阅读脚本、参考资料或模板。不会把所有正文和代码预先拼入提示词。子Agent只能看到所选技能，当前仍没有工作区shell。

根任务首次执行冻结正文、脚本和资源字节及哈希，非UTF-8资源以Base64保存在同一JSON快照。修改部署文件影响新根任务，旧任务继续原内容。每文件最多1MiB，整体最多8MiB/2048文件；超限报错，不静默漏掉依赖。排除隐藏项、Python缓存及node_modules，拒绝符号链接和越界路径。

脚本不自动导入、执行或安装依赖，不必逐个注册为模型工具。主Agent取得现有execute权限后，可以运行：

```sh
python "$SKILLS_DIR/github-deep-research/scripts/github_api.py" langchain-ai deepagents info
```

SKILLS_DIR指向本次命令临时生成的固定包目录，包括同版本的脚本和相对资源，调用结束清理。文件工具仍用`/skills/...`虚拟路径；命令工作目录仍是挂载区，输出写`outputs/...`。不保存临时地址供下次调用。没有execute权限或仅知识库模式时不能运行脚本；命令继续走超时、取消、预算与输出管理，不继承应用密钥。

只有实际成功读取的包才为后续命令准备资源。RunSkillUse保存最小读取事实，skill_materialized仅是资源准备事件，不算新增使用；普通命令不会把全部技能标为用过。停用、移除与撤回阻断已使用包的后续调用，重新开启不恢复旧许可；普通更新不影响旧任务固定脚本。历史不会被“擦除”，本地shell仍有所在容器的权限，目录只读标记不构成抵抗恶意代码的强隔离。

## 新增与启用

Web独立Skills页提供导入、检查/试读、启停、详情、更新、导出和移除；工具与角色选择器继续共用同一目录。availability.json是部署兼容性上限，PG中的ToolPolicy.skill_enabled保存用户选择，最终必须两者都允许。关闭会撤回旧运行读取/执行资源的能力，重新开启只供新运行使用；已读取内容不从历史中抹除。子Agent的持久角色可保留已关闭技能名，实际运行只装配根运行获准的技能。

推荐通过页面导入单SKILL.md、完整目录或ZIP。名称/用途缺失可补填，正文说明方法、工具与脚本依赖，不要求统一正文模板。确认前只暂存与试读，确认后新包默认停用，核对使用条件后单独启用；普通更新替换整个包并保持原开关。metadata.version可选，没有时使用入口摘要显示版本。页面外改过当前文件时先导出保留，再重新上传检查后确认替换；预览之后再改文件会触发冲突。导出保留完整脚本与资源，移除不改写历史运行快照。

管理接口需要0041及已有包接管；MCP完整管理继续升级至0042。当前正式库已完成0042迁移，12个现有包已接管且内容、开关保持。全新安装从`skill-seeds/`复制初始模板，已有部署只接管现存包；重复初始化不会覆盖用户修改或恢复已删除的包。暂存与恢复记录位于`.management`，仅供应用使用，不应手动修改；未提交上传一天到期，终态操作记录默认三十天清理。

更换整个目录用.env的SKILLS_HOST_DIRECTORY，容器路径由部署提供。包使用已安装的Python/Bash/Node等环境；支持带脚本的技能不意味着其依赖都已安装。没有自动凭据绑定或Skill自我安装。

Skill里的allowed-tools不是本应用的授权来源；包含本轮未支持的权限、钩子或MCP声明会显示不兼容并保持停用。真正可用工具仍由统一工具策略、根任务限制与角色许可计算；不要靠复制声明扩大权限。支持脚本不等于安装第三方应用内部模块或自动满足依赖。

## 来源与采用方式

前轮两份移植方法基于[DeerFlow c89fed0](https://github.com/bytedance/deer-flow/tree/c89fed0be4c64b4d881942a0fd240f239e239e96/skills/public)。本轮检查和适配用户复制的包，核对上游最新提交为[3343522](https://github.com/bytedance/deer-flow/tree/3343522fa2e62b1a1bb7df430affcba647103daf)。保留LICENSE-DeerFlow与frontend-design/LICENSE.txt；改动正文注明KnowledgeOps适配。

运行侧直接使用Deep Agents 0.7.23。包边界参考DeerFlow的LocalSkillStorage及test_load_skills_stops_at_skill_package_boundary；启用资源投影参考skills/projection.py，但本应用投影固定运行快照。没有安装整套deerflow-harness，其广泛的模型、沙盒、存储与运行设施不是此次修复所必需。
