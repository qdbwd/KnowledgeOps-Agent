"""文本/扫描/混合 PDF 使用真实本地解析与 Tesseract，不调用模型。"""

import importlib
import io
from pathlib import Path

import pytest
from PIL import Image, ImageDraw, ImageFont
from pypdf import PdfWriter
from reportlab.pdfgen.canvas import Canvas

from knowledgeops_agent.sources.service import SourceService


def text_pdf() -> bytes:
    output = io.BytesIO()
    canvas = Canvas(output)
    canvas.drawString(60, 740, "KnowledgeOps source fixture. Default tool budget is 80 attempts.")
    canvas.drawString(
        60, 715, "Restart requires explicit user approval before further model calls."
    )
    canvas.save()
    return output.getvalue()


def scan_pdf(language: str) -> bytes:
    image = Image.new("RGB", (1654, 1000), "white")
    draw = ImageDraw.Draw(image)
    path = Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc")
    font = ImageFont.truetype(str(path), 50)
    lines = (
        [
            "KnowledgeOps research material",
            "The maximum budget is 80 tool attempts.",
            "Restart requires explicit approval.",
        ]
        if language == "eng"
        else ["知识库研究材料", "工具执行预算最多为80次。", "服务重启后需要用户明确恢复。"]
    )
    for index, line in enumerate(lines):
        draw.text((75, 130 + index * 100), line, font=font, fill="black")
    output = io.BytesIO()
    image.save(output, format="PDF", resolution=150)
    image.close()
    return output.getvalue()


@pytest.mark.parametrize("kind", ["text", "eng", "chi_sim", "mixed"])
def test_real_pdf_pages_are_located_and_ocr_is_used_only_when_needed(
    business_sessions, tmp_path, kind
):
    parser = importlib.import_module("knowledgeops_agent.sources.pdf")
    sources = SourceService(business_sessions, tmp_path)
    if kind == "text":
        data = text_pdf()
    elif kind == "mixed":
        writer = PdfWriter()
        writer.append(io.BytesIO(text_pdf()))
        writer.append(io.BytesIO(scan_pdf("chi_sim")))
        stream = io.BytesIO()
        writer.write(stream)
        data = stream.getvalue()
    else:
        data = scan_pdf(kind)
    uploaded = sources.register_pdf("固定 PDF 样本", data)
    assert uploaded["status"] == "pending"
    assert sources.register_pdf("重复上传", data)["version_id"] == uploaded["version_id"]
    progress = []
    ready = parser.parse_pdf(
        sources, uploaded["version_id"], progress=lambda item: progress.append(item)
    )
    assert ready["status"] == "ready"
    assert sources.full_content(ready["version_id"])["integrity"] == "verified"
    texts = [sources.get_segment(item["id"])["text"] for item in ready["segments"]]
    assert "80" in "\n".join(texts)
    if kind in {"chi_sim", "mixed"}:
        assert "恢复" in "\n".join(texts)
    assert {item["method"] for item in ready["segments"]} == (
        {"text"} if kind == "text" else {"text", "ocr"} if kind == "mixed" else {"ocr"}
    )
    assert sorted({item["page"] for item in ready["segments"]}) == (
        [1, 2] if kind == "mixed" else [1]
    )
    read = sources.read_segment(ready["segments"][-1]["id"], root_id="pdf-test")
    assert read["page"] == (2 if kind == "mixed" else 1)
    assert sources.original(read["source_version_id"])[0] == data
    # 已完成页的恢复不会再 OCR，也不会复制片段。
    assert parser.parse_pdf(sources, uploaded["version_id"])["segments"] == ready["segments"]
    assert len(progress) == (2 if kind == "mixed" else 1)


def test_corrupt_pdf_is_recorded_as_failed_not_as_empty_success(business_sessions, tmp_path):
    parser = importlib.import_module("knowledgeops_agent.sources.pdf")
    sources = SourceService(business_sessions, tmp_path)
    uploaded = sources.register_pdf("损坏文件", b"%PDF-corrupt data")
    with pytest.raises(ValueError, match="PDF"):
        parser.parse_pdf(sources, uploaded["version_id"])
    failed = sources.get_source(uploaded["id"])
    assert failed["status"] == "failed" and failed["error"]
    assert failed["segments"] == []


def test_pdf_resume_preserves_completed_page_and_failed_page_cannot_be_cited(
    business_sessions, tmp_path, monkeypatch
):
    parser = importlib.import_module("knowledgeops_agent.sources.pdf")
    sources = SourceService(business_sessions, tmp_path)
    writer = PdfWriter()
    writer.append(io.BytesIO(text_pdf()))
    writer.add_blank_page(width=595, height=842)
    stream = io.BytesIO()
    writer.write(stream)
    uploaded = sources.register_pdf("逐页恢复", stream.getvalue())
    calls = 0

    def stop_before_second_page():
        nonlocal calls
        calls += 1
        if calls == 4:
            raise RuntimeError("simulated task interruption")

    with pytest.raises(RuntimeError, match="interruption"):
        parser.parse_pdf(sources, uploaded["version_id"], check=stop_before_second_page)
    partial = sources.get_source(uploaded["id"])
    first_id = partial["segments"][0]["id"]
    assert partial["status"] == "processing" and len(partial["segments"]) == 1
    monkeypatch.setattr(parser, "_ocr_page", lambda *_: "")
    result = parser.parse_pdf(sources, uploaded["version_id"])
    assert result["status"] == "partial" and result["segments"][0]["id"] == first_id
    with pytest.raises(ValueError, match="unavailable"):
        sources.read_segment(result["segments"][1]["id"], root_id="root")
    monkeypatch.setattr(parser, "_ocr_page", lambda *_: "Recovered readable page with 80 attempts.")
    result = parser.parse_pdf(sources, uploaded["version_id"])
    assert result["status"] == "ready" and result["segments"][0]["id"] == first_id
