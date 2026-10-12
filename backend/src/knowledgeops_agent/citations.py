"""统一识别正文里的应用引用；存在性、权限和已读验证仍由业务服务执行。"""

import re
from collections.abc import Collection

# 同时处理 Markdown 链接、行内代码和中文正文邻接的 URL。不能只匹配合法
# UUID，否则拼错/伪造的 ID 会被忽略，无法交给调用端拒绝。
EVIDENCE_URL = re.compile(r"/api/evidence/([^\s)\]<>#?`\"'，。；：！？（）]+)")
MARKER = re.compile(r"\[citation\b([^\]\n]*)(?:\]|(?=\n|$))", re.IGNORECASE)
EVIDENCE_LINK = re.compile(r"\[[^\]\n]*\]\(/api/evidence/([^\s)]+)\)")


def _prose(text: str) -> str:
    """仅为占位符/可点击检查遮蔽代码，保留位置以便无损替换正文。"""
    lines, fence = [], None
    for line in text.splitlines(keepends=True):
        opening = re.match(r"^ {0,3}(`{3,}|~{3,})", line)
        if fence is not None:
            lines.append(" " * len(line))
            if re.fullmatch(
                r" {0,3}" + re.escape(fence[0]) + "{" + str(len(fence)) + r",}\s*", line
            ):
                fence = None
        elif opening:
            fence = opening[1]
            lines.append(" " * len(line))
        else:
            lines.append(line)
    prose = "".join(lines)
    return re.sub(
        r"(?P<ticks>`+)(?!`).*?(?P=ticks)(?!`)", lambda m: " " * len(m[0]), prose, flags=re.DOTALL
    )


def _markers(text: str) -> list[re.Match]:
    prose = _prose(text)
    # [citation] 本身也可作为真实 Markdown 链接的标签；只识别未链接的标记。
    return [
        match
        for match in MARKER.finditer(prose)
        if not re.match(r"\(/api/evidence/[^\s)]+\)", prose[match.end() :])
    ]


def _marker_id(match: re.Match) -> str | None:
    payload = match[1].strip()
    return payload[1:].strip() if payload.startswith(":") and payload[1:].strip() else None


def citation_placeholders(text: str) -> list[str]:
    """返回正文中仍未渲染的 citation 标记；代码示例不属于来源性断言。"""
    return [match[0] for match in _markers(text)]


def render_citation_markers(text: str, verified_ids: Collection[str]) -> str:
    """将已验证的 ID 标记渲染为应用链接，不猜测缺失 ID 的来源。

    调用端必须先验证真实证据和任务归属/继承关系；集合不是模型自报的目录。
    无 ID、未闭合或集合外标记拒绝整段正文，让原工具/回答修订机制处理。
    """
    markers = _markers(text)
    for match in reversed(markers):
        evidence_id = _marker_id(match)
        if not match[0].endswith("]") or evidence_id not in verified_ids:
            raise ValueError(
                "unresolved citation placeholder: use a verified evidence citation link"
            )
        text = text[: match.start()] + f"[来源](/api/evidence/{evidence_id})" + text[match.end() :]
    return text


def clickable_evidence_ids(text: str) -> list[str]:
    """供正文级验收识别真正可点击的 Markdown 引用，代码内 URL 不计入。"""
    return sorted(set(EVIDENCE_LINK.findall(_prose(text))))


def evidence_ids(text: str) -> list[str]:
    """返回稳定去重的候选 ID；格式、来源归属和可解析性由消费方校验。"""
    ids = set(EVIDENCE_URL.findall(text))
    ids.update(evidence_id for match in _markers(text) if (evidence_id := _marker_id(match)))
    return sorted(ids)


def render_answer_citations(text: str, verified_ids: Collection[str]) -> str:
    """为回答中已提及的真实证据补齐入口，不改写代码示例或事实表述。

    verified_ids 必须由调用端按原权限规则查验，不能直接使用模型自报值。
    仅补充本段确实出现、却没有可点击入口的ID；不把整个已读目录拼进回答，
    也不推断某个来源支持哪些结论。知识正文仍要求作者在论断旁正确引用。
    """
    rendered = render_citation_markers(text, verified_ids)
    missing = (set(evidence_ids(text)) & set(verified_ids)) - set(clickable_evidence_ids(rendered))
    if not missing:
        return rendered
    links = [
        f"- [查看证据 {number}](/api/evidence/{evidence_id})"
        for number, evidence_id in enumerate(sorted(missing), start=1)
    ]
    rendered += "\n\n文中提到的证据：\n" + "\n".join(links)
    # 未闭合围栏可能把追加内容继续包成代码。不能宣称已经修复，也不擅自
    # 改写示例；沿用回答修订反馈，让模型明确补齐 Markdown 结构。
    if missing - set(clickable_evidence_ids(rendered)):
        raise ValueError("evidence links remain inside an unclosed code block")
    return rendered
