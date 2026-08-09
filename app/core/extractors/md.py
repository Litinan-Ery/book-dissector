"""Markdown 文本提取：保留标题层级结构（# 级数 → level）。"""
from __future__ import annotations

import re
from pathlib import Path

from charset_normalizer import from_bytes

from .base import Chapter, ExtractResult
from ..modalities import detect_text_modalities


def extract(path: Path) -> ExtractResult:
    raw = path.read_bytes()
    try:
        text = from_bytes(raw).best().output(encoding="utf-8").decode("utf-8")
    except Exception:
        text = raw.decode("utf-8", errors="replace")

    if not text.strip():
        return ExtractResult(error="文件内容为空")

    # YAML frontmatter 中的 title / author
    title = ""
    author = ""
    fm = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, flags=re.S)
    if fm:
        for ln in fm.group(1).splitlines():
            m = re.match(r"^\s*(title|author)\s*:\s*(.+?)\s*$", ln)
            if m:
                val = m.group(2).strip().strip("\"'")
                if m.group(1) == "title":
                    title = val
                else:
                    author = val

    # 章节：ATX 标题
    chapters: list[Chapter] = []
    for m in re.finditer(r"^(#{1,6})\s+(.+?)\s*$", text, flags=re.M):
        chapters.append(
            Chapter(
                title=m.group(2).strip(),
                level=len(m.group(1)),
                start_char=m.start(),
                end_char=-1,
            )
        )
    for i, ch in enumerate(chapters):
        ch.end_char = chapters[i + 1].start_char if i + 1 < len(chapters) else len(text)

    if not title:
        title = chapters[0].title if chapters else path.stem
    return ExtractResult(
        title=title,
        author=author,
        source_format="md",
        text=text,
        chapters=chapters,
        modality_warnings=detect_text_modalities(text),
    )
