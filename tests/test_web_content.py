"""网页保存正文而非 HTML；版本、结构和完整性使用真实解析器与数据库验证。"""

import hashlib
from unittest.mock import Mock

import httpx
import pytest
from sqlalchemy import select

from knowledgeops_agent.persistence.models import Source, SourceSegment, SourceVersion
from knowledgeops_agent.sources.service import SourceService
from knowledgeops_agent.sources.web import PublicWebReader, WebResearchService


def documentation_html():
    return (
        """<!doctype html><html><head><title>恢复机制</title></head><body>
    <nav>不属于正文的导航</nav><main><article><h1>恢复机制</h1>
    <p>检查点保存图的运行状态，恢复时使用原来的会话标识。
    下面的程序建立带检查点保存器的图，不能将它误解成重新执行已经完成的副作用。</p>
    <h2>建立图</h2><pre><code class="language-python">graph = builder.compile(checkpointer=saver)
result = graph.invoke(inputs, config)</code></pre>
    <p>调用方式与状态表配合使用，原始请求和结果需要分别核对。</p>
    <table><thead><tr><th>状态</th><th>含义</th></tr></thead>
    <tbody><tr><td>paused</td><td>等待用户继续</td></tr>
    <tr><td>completed</td><td>任务已完成</td></tr></tbody></table>
    <ul><li>保留固定引用。</li><li>避免重复执行。</li></ul>
    <p>进一步阅读 <a href="https://example.org/reference">参考文档</a>。</p>
    </article></main><script>window.secret = 'must-not-save';</script></body></html>"""
    ).encode()


def web_service(sources, html):
    return WebResearchService(
        sources,
        reader=PublicWebReader(
            resolver=lambda *_: ["93.184.215.14"],
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    200,
                    content=html,
                    headers={"content-type": "text/html"},
                )
            ),
        ),
        searcher=Mock(),
    )


def test_web_body_preserves_code_table_headings_and_never_saves_html(business_sessions, tmp_path):
    sources = SourceService(business_sessions, tmp_path)
    source, _ = web_service(sources, documentation_html()).import_url("https://example.org/docs")
    content = sources.full_content(source["version_id"])
    assert "builder.compile(checkpointer=saver)" in content["text"]
    assert "graph.invoke(inputs, config)" in content["text"]
    assert "## 建立图" in content["text"]
    assert "```" in content["text"]
    assert "|" in content["text"] and "等待用户继续" in content["text"]
    assert "https://example.org/reference" in content["text"]
    assert "must-not-save" not in content["text"]
    assert source["body_format"] == "markdown" and not source["original_available"]
    assert content["body_hash"] == hashlib.sha256(content["text"].encode()).hexdigest()
    assert not any(path.is_file() for path in tmp_path.rglob("*"))
    with business_sessions() as session:
        version = session.get(SourceVersion, source["version_id"])
        assert version.storage_key is None and version.original_hash is None


def test_web_deduplicates_body_and_parser_not_changing_html(business_sessions, tmp_path):
    sources = SourceService(business_sessions, tmp_path)
    fields = {"kind": "web", "origin": "https://example.org/docs", "parser": "markdown-v1"}
    first = sources.import_text("标题", "完整正文", raw_bytes=b"<html>one</html>", **fields)
    same = sources.import_text("标题", "完整正文", raw_bytes=b"<html>two</html>", **fields)
    assert first["version_id"] == same["version_id"]
    different = sources.import_text("标题", "正文有修改", **fields)
    assert different["version_id"] != first["version_id"]
    parser = sources.import_text("标题", "完整正文", **{**fields, "parser": "markdown-v2"})
    assert parser["version_id"] != first["version_id"]
    assert sources.full_content(first["version_id"])["text"] == "完整正文"


def test_legacy_html_and_evidence_remain_immutable_when_new_body_is_imported(
    business_sessions, tmp_path
):
    sources = SourceService(business_sessions, tmp_path)
    data = documentation_html()
    with business_sessions.begin() as session:
        digest, key = sources._store_original(data, session=session)
        source = Source(
            identity_key="web:https://example.org/docs",
            kind="web",
            title="旧网页",
            origin="https://example.org/docs",
        )
        session.add(source)
        session.flush()
        version = SourceVersion(
            source_id=source.id,
            number=1,
            content_hash=digest,
            storage_key=key,
            mime_type="text/html",
            parser="trafilatura-v2",
        )
        session.add(version)
        session.flush()
        segment = SourceSegment(
            source_version_id=version.id,
            position=0,
            text="旧的已引用正文",
            start_offset=0,
            end_offset=len("旧的已引用正文"),
        )
        session.add(segment)
        session.flush()
        old_version, old_segment = version.id, segment.id
    evidence = sources.read_segment(old_segment, root_id="legacy-read")
    fresh, _ = web_service(sources, data).import_url("https://example.org/docs", refresh=True)
    assert fresh["version"] == 2
    assert sources.full_content(old_version)["text"] == "旧的已引用正文"
    assert sources.get_evidence(evidence["id"])["text"] == "旧的已引用正文"
    assert sources.original(old_version)[0] == data  # 迁移内部仍能核对，HTTP 不再下载 HTML。


def test_saved_body_corruption_is_not_silently_rendered(business_sessions, tmp_path):
    sources = SourceService(business_sessions, tmp_path)
    first = sources.import_text(
        "标题", "未损坏的完整正文", kind="web", origin="https://example.org"
    )
    with business_sessions.begin() as session:
        segment = session.scalar(
            select(SourceSegment).where(SourceSegment.source_version_id == first["version_id"])
        )
        segment.text = "遭到改写的正文"
    with pytest.raises(ValueError, match="integrity"):
        sources.full_content(first["version_id"])
    with pytest.raises(ValueError, match="integrity"):
        sources.read_segment(first["segments"][0]["id"], root_id="corrupt-read")


def test_plain_web_text_keeps_literal_markdown_characters(business_sessions, tmp_path):
    sources = SourceService(business_sessions, tmp_path)
    service = WebResearchService(
        sources,
        reader=PublicWebReader(
            resolver=lambda *_: ["93.184.215.14"],
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    200,
                    text="# This is plain text, not a heading.\n" * 4,
                    headers={"content-type": "text/plain"},
                )
            ),
        ),
    )
    source, _ = service.import_url("https://example.org/plain")
    assert source["body_format"] == "text"
    assert sources.full_content(source["version_id"])["text"].startswith("# This is plain text")
