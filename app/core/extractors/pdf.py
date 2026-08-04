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
    try:
        for page in doc:
            parts.append(page.get_text("text"))
    finally:
        doc.close()

    text = "\n".join(parts)
    if len(text.strip()) < 50:
        return ExtractResult(
            title=title,
            author=author,
            source_format="pdf",
            error="提取到的文本过少，可能是扫描版 PDF（需要 OCR，当前不支持）",
        )
    return ExtractResult(title=title, author=author, source_format="pdf", text=text)
