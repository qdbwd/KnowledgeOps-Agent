# 0015：辅助模型按需启用与委派范围澄清

日期：2026-10-09。依据用户本轮明确反馈，调整使用默认值和投入优先级，不扩大权限、模型渠道或额度。

后续修订：子Agent配置入口已由0016纳入本轮；用户随后按[0019](0019-remove-auxiliary-experiment-gate.md)取消关闭的辅助工具的效果实验要求。下文“20条辅助对照尚未完成”保留为历史，不再构成当前交付阻塞。默认关闭、权限及其他能力边界继续适用。

## 已确认与建议的区别

- 用户提出：辅助模型若收益一般，平时可先禁用；需要的是多智能体能力，不要求任务必须用多智能体完成。网络研究子智能体不是独立刚需，不再围绕它深入研究优化。用户随后将“复制任务”纠正为“复杂任务”。
- 本轮采用：call_model保留实现，新根任务默认关闭，Web/API显式选择后提供。主/子Agent共用根任务选择；隐藏工具与服务入口校验同时执行。MODEL_CALLS_ENABLED仍控制全部收费调用，不另增一个同名含糊开关。
- 保留现有有限只读委派、独立上下文/检查点、共享根预算、最多两个并发、禁止递归及恢复复用；不强制使用，不把委派能力等同于独立的无人网络研究产品。
- 当前子Agent能检索知识库、读取材料，在允许联网时搜索网页，任务说明由主Agent动态提供；它不是只能上网。新增角色配置入口和主Agent自动生成持久角色仅是讨论建议，尚未成为本轮必需开发项。

## 参考核对

Deep Agents固定参考6a3a12bc的[SubAgent/CompiledSubAgent与task实现](https://github.com/langchain-ai/deepagents/blob/6a3a12bc5b3ac88aeb091e6c77ba81ad4d6732af/libs/deepagents/deepagents/middleware/subagents.py)接受预先提供的定义；task按subagent_type选择并传入动态description。官方[动态子Agent说明](https://docs.langchain.com/oss/python/deepagents/dynamic-subagents)讲解释器对已配置角色进行循环/分支/并行编排，不可据名称推断自由创建角色。

DeerFlow 2.0固定7797409的[子Agent目录](https://github.com/bytedance/deer-flow/blob/7797409355cbf257923a81ab9781b706c38f9c3c/frontend/src/content/zh/harness/subagents/catalog.mdx)、[委派工具](https://github.com/bytedance/deer-flow/blob/7797409355cbf257923a81ab9781b706c38f9c3c/backend/packages/harness/deerflow/tools/builtins/task_tool.py)、registry及test_subagents_router已只读核对：内置/config.yaml/管理员管理定义合成目录，task选择已存在且允许的类型；管理员API可增改角色，不等于主模型默认获得管理员能力。本项目借鉴预设能力与动态分工的分离，不照搬角色管理平台。

## 验收与证据边界

- 未启用call_model时主/子均不见该工具，直接服务调用同样拒绝且不计费；显式启用后原材料权限、根预算、成功复用和未知结果拒绝重发仍成立。
- 旧运行缺失auxiliary选择按false处理，不重写历史；历史工具调用不会因此假装未发生。需要再使用时由用户开始显式启用的新任务。
- 辅助对照和网络研究不继续扩大投入，优先修复知识库主业务及授权错误。已经执行的实验保存完整失败和费用，不宣称统计上证明无效。
- 原20条辅助对照及当前版本委派重验尚未全部完成，记录未完成，不把降低优先级伪装为满足旧验收合同；其余24例门槛、交互和工程条件不变。
