"""PDF 文本提取：pymupdf 逐页提取；元数据取标题/作者；章节切分留待 M3。"""
from __future__ import annotations

from pathlib import Path

import fitz

from .base import ExtractResult
from ..modalities import detect_text_modalities, merge_warnings


def extract(path: Path) -> ExtractResult:
    try:
        doc = fitz.open(str(path))
    except Exception as exc:
        return ExtractResult(error=f"PDF 打开失败：{exc.__class__.__name__}")

    meta = doc.metadata or {}
    title = (meta.get("title") or "").strip()
    author = (meta.get("author") or "").strip()

    parts: list[str] = []
    modality_warnings: list[dict] = []
    try:
        for page_number, page in enumerate(doc, start=1):
            page_text = page.get_text("text")
            parts.append(page_text)
            modality_warnings.extend(
                detect_text_modalities(page_text, location=f"第 {page_number} 页")
            )
            image_count = len(page.get_images(full=True))
            if image_count:
                modality_warnings.append(
                    {
                        "type": "image",
                        "count": image_count,
                        "location": f"第 {page_number} 页",
                        "message": "检测到图片；当前不能理解图片语义",
                    }
                )
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
    return ExtractResult(
        title=title,
        author=author,
        source_format="pdf",
        text=text,
        modality_warnings=merge_warnings(modality_warnings),
    )
