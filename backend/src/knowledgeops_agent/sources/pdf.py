"""分页文本提取与本地 OCR；页结果逐次落库，失败页不会生成已读证据。

扫描页先由 PDFium 渲染，再调用固定 Tesseract 命令。没有通用 shell 执行，
不执行 PDF 中的脚本。根任务停止检查由调用方注入，OCR 不消耗模型预算。
"""

from __future__ import annotations

import hashlib
import io
import subprocess
from threading import Lock

import pypdfium2
from pypdf import PdfReader
from sqlalchemy import delete, func, select

from knowledgeops_agent.persistence.models import SourceSegment, SourceVersion
from knowledgeops_agent.sources.service import SourceService

_PDFIUM_LOCK = Lock()  # PDFium 不支持跨线程并发调用，即使处理不同文档也需保护。


def _ocr_page(data: bytes, page_index: int) -> str:
    """渲染像素和子进程时间有上限；图像仅存在内存，不产生临时敏感文件。"""
    with _PDFIUM_LOCK, pypdfium2.PdfDocument(data) as document:
        page = document[page_index]
        width, height = page.get_size()
        scale = 2.5  # 180 dpi，普通扫描件的速度与识别质量折中。
        if width * height * scale * scale > 20_000_000:
            raise ValueError("PDF page exceeds rendering pixel limit")
        bitmap = page.render(scale=scale)
        image = bitmap.to_pil()
        try:
            buffer = io.BytesIO()
            image.save(buffer, format="PNG")
        finally:
            image.close()
            bitmap.close()
            page.close()
    result = subprocess.run(
        ["tesseract", "stdin", "stdout", "-l", "chi_sim+eng", "--psm", "3"],
        input=buffer.getvalue(),
        capture_output=True,
        timeout=35,
        check=False,
    )
    if result.returncode:
        raise ValueError("OCR process failed; check installed language data")
    return result.stdout.decode("utf-8", errors="replace").strip()


def _set_status(sources: SourceService, version_id: str, status: str, error: str | None = None):
    with sources.sessions.begin() as session:
        version = session.scalar(
            select(SourceVersion).where(SourceVersion.id == version_id).with_for_update()
        )
        if version is None:
            raise KeyError("source version not found")
        version.status, version.error = status, error
        if status == "ready":
            # 仅完整解析完成后封存正文摘要；失败页重试前不伪造完整性证明。
            body = "".join(session.scalars(select(SourceSegment.text).where(
                SourceSegment.source_version_id == version_id,
            ).order_by(SourceSegment.page.asc().nulls_first(), SourceSegment.position)))
            version.body_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()


def _save_page(
    sources: SourceService, version_id: str, page: int, content: str, method: str, error: str | None
):
    """页级原子保存：恢复跳过成功页，重试失败页不改动已被引用的片段。"""
    with sources.sessions.begin() as session:
        session.scalar(
            select(SourceVersion).where(SourceVersion.id == version_id).with_for_update()
        )
        existing = list(
            session.scalars(
                select(SourceSegment).where(
                    SourceSegment.source_version_id == version_id, SourceSegment.page == page
                )
            )
        )
        if any(row.status == "ready" for row in existing):
            return
        session.execute(
            delete(SourceSegment).where(
                SourceSegment.source_version_id == version_id, SourceSegment.page == page
            )
        )
        position = session.scalar(
            select(func.max(SourceSegment.position)).where(
                SourceSegment.source_version_id == version_id
            )
        )
        position = 0 if position is None else position + 1
        for offset in range(0, max(1, len(content)), 2400):
            piece = content[offset : offset + 2400]
            session.add(
                SourceSegment(
                    source_version_id=version_id,
                    position=position,
                    page=page,
                    start_offset=offset,
                    end_offset=offset + len(piece),
                    text=piece,
                    method=method,
                    status="failed" if error else "ready",
                    error=error,
                )
            )
            position += 1


def parse_pdf(
    sources: SourceService, version_id: str, *, check=lambda: None, progress=lambda _item: None
) -> dict:
    """解析最多 40 页的普通 PDF。输入为已上传版本，输出包含每页方法/错误。

    每页先尝试文本提取，文本不足再 OCR；复杂表格、公式和手写内容不宣称
    已可靠还原。每页开始/提交前检查取消，重启后保留已完成页以供明确恢复。
    """
    version = sources.get_version(version_id)
    if version["mime_type"] != "application/pdf":
        raise ValueError("source is not a PDF")
    if version["status"] == "ready":
        return version
    check()
    data, _ = sources.original(version_id)
    try:
        reader = PdfReader(io.BytesIO(data), strict=False)
        if reader.is_encrypted:
            raise ValueError("encrypted PDF is not supported")
        if not 1 <= len(reader.pages) <= 40:
            raise ValueError("PDF must contain between 1 and 40 pages")
    except Exception as exc:
        message = (
            str(exc)
            if isinstance(exc, ValueError)
            else f"PDF could not be opened ({type(exc).__name__})"
        )
        _set_status(sources, version_id, "failed", message)
        raise ValueError(message) from None
    _set_status(sources, version_id, "processing")
    completed_pages = {item["page"] for item in version["segments"] if item["status"] == "ready"}
    for index, page in enumerate(reader.pages):
        check()
        if index + 1 in completed_pages:
            continue
        method, content, error = "text", "", None
        try:
            try:
                content = (page.extract_text() or "").strip()
            except Exception:
                content = ""
            if len(content) < 40:
                method, content = "ocr", _ocr_page(data, index)
            if not content.strip():
                raise ValueError("no readable text; blank page or unsupported scan")
            if len(content) > 200000:
                raise ValueError("PDF page text size limit exceeded")
        except Exception as exc:
            content = ""
            error = (
                str(exc)
                if isinstance(exc, ValueError)
                else f"PDF page extraction failed ({type(exc).__name__})"
            )
        check()
        _save_page(sources, version_id, index + 1, content, method, error)
        progress(
            {
                "page": index + 1,
                "pages": len(reader.pages),
                "method": method,
                "status": "failed" if error else "ready",
                "error": error,
            }
        )
    result = sources.get_version(version_id)
    failed = sum(item["status"] != "ready" for item in result["segments"])
    ready = any(item["status"] == "ready" for item in result["segments"])
    _set_status(
        sources,
        version_id,
        "partial" if failed and ready else "failed" if failed else "ready",
        "部分页面无法可靠提取，请查看逐页状态；复杂表格、公式与手写内容需核对原件。"
        if failed
        else None,
    )
    return sources.get_version(version_id)
