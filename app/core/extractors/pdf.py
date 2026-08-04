"""PDF 文本提取：pymupdf 逐页提取；元数据取标题/作者；章节切分留待 M3。"""
from __future__ import annotations

from pathlib import Path

import fitz

from .base import ExtractResult


def extract(path: Path) -> ExtractResult:
    try:
        doc = fitz.open(str(path))
    except Exception as exc:
        return ExtractResult(error=f"PDF 打开失败：{exc.__class__.__name__}")

    meta = doc.metadata or {}
    title = (meta.get("title") or "").strip()
    author = (meta.get("author") or "").strip()

    parts: list[str] = []
    raw_text_chars = 0
    try:
        for page_number, page in enumerate(doc, start=1):
            page_text = page.get_text("text")
            raw_text_chars += len(page_text.strip())
            markers: list[str] = []
            image_count = len(page.get_images(full=True))
            if image_count:
                markers.append(f"[图片：第 {page_number} 页，共 {image_count} 幅]")
            if markers:
                page_text = page_text.rstrip() + "\n" + "\n".join(markers) + "\n"
            parts.append(page_text)
    finally:
        doc.close()

    text = "\n".join(parts)
    if raw_text_chars < 50:
        return ExtractResult(
            title=title,
            author=author,
            source_format="pdf",
            text=text,
            error="提取到的文本过少，可能是扫描版 PDF（需要 OCR，当前不支持）",
        )
    return ExtractResult(title=title, author=author, source_format="pdf", text=text)
