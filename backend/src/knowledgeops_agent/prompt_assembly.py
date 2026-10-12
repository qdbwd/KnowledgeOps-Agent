"""参考DeerFlow apply_prompt_template的分区装配方式，保留本项目自己的业务规则。

这里只组装稳定系统指令；动态任务背景由RuntimeControlMiddleware组织，历史由
ContextMiddleware选取工作视图，检索原文通过ToolMessage进入下一次模型输入。
资源清单冻结模板全文及检索说明，恢复旧运行不会自动套上新模板。
"""

from html import escape

SYSTEM_TEMPLATE = """<application_policy>
{policy}
</application_policy>

<operating_instructions>
{instructions}
</operating_instructions>

<retrieval_guidance>
{retrieval}
</retrieval_guidance>{role}{read_only}"""

RETRIEVAL_GUIDANCE = """按问题选择可用检索入口，不把各类数据混成同一种记忆：
- “我之前要求过什么、刚才怎样解释”：history_search找当前会话可达原话；使用返回的id与start，调用history_read(source_id=id, offset=start)核对原角色与上下文。
- “知识库文章怎样说明”：search_knowledge检索已发布正文，未在聊天展示的正文也能命中。使用document_id、version_id、start调用read_knowledge取得固定版本证据，再按next_start补读；修改前从start=0完整读取。该工具没有length参数；目录和历史助手回答不能代替知识正文。
- 已明确生效的用户偏好与工作笔记在任务背景中提供；笔记只是模型报告，旧偏好有适用范围，均不产生新授权。
- 搜索返回的是词法候选及局部摘录，可能只有部分查询词匹配。补读后检查条件、否定、数值和版本；不足时换一组具体关键词再查，查不到不表示原文不存在。
检索结果经工具反馈进入下一次调用；无须把全部历史再改写成新的系统指令。只调用本次实际开放的工具，不因本说明提到某工具就假定已获许可。"""

READ_ONLY_INSTRUCTION = "你是只读研究子 Agent，仅完成委派问题，返回简洁结论、读取工具提供的引用和未决项。不能维护或发布知识。"


def assemble_system_prompt(
    policy_text: str, prompts: dict, *, role_prompt: str | None, read_only: bool
) -> str:
    """新任务使用被冻结的分区模板；旧快照逐字保留原装配合同。

    自定义角色内容按上游SOUL渲染做标签转义，避免伪造分区边界；转义并不能
    代替实际工具权限检查。核心规则来自项目受控文件，不接受用户材料替换。
    """
    template = prompts.get("assembly_template")
    if template is None:
        return (
            policy_text
            + "\n\n"
            + prompts["system"]
            + ("\n角色专属指令：\n" + role_prompt if role_prompt is not None else "")
            + ("\n" + READ_ONLY_INSTRUCTION if read_only else "")
        )
    return template.format(
        policy=policy_text,
        instructions=prompts["system"],
        retrieval=prompts["retrieval"],
        role=("\n\n<configured_role>\n" + escape(role_prompt, quote=False) + "\n</configured_role>")
        if role_prompt is not None
        else "",
        read_only=("\n\n<read_only_boundary>\n" + READ_ONLY_INSTRUCTION + "\n</read_only_boundary>")
        if read_only
        else "",
    )
