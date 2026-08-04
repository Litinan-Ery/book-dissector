"""TXT 文本提取：编码自动检测；按"第X章/Chapter X"模式切分章节。"""
from __future__ import annotations

import re
from pathlib import Path

from charset_normalizer import from_bytes

from .base import CHAPTER_RE, Chapter, ExtractResult


def extract(path: Path) -> ExtractResult:
    raw = path.read_bytes()
    try:
        text = from_bytes(raw).best().output(encoding="utf-8").decode("utf-8")
    except Exception:
        try:
            text = raw.decode("utf-8", errors="replace")
        except Exception as exc:
            return ExtractResult(error=f"编码识别失败：{exc.__class__.__name__}")

    if not text.strip():
        return ExtractResult(error="文件内容为空")

    lines = text.splitlines()
    # 首行非空作为书名（启发式，允许覆盖）
    title = next((ln.strip() for ln in lines if ln.strip()), "")

    # 章节切分
    chapters: list[Chapter] = []
    buf_start = 0
    for i, ln in enumerate(lines):
        m = CHAPTER_RE.match(ln)
        if m:
            start = _offset(lines, i)
            if chapters:
                chapters[-1].end_char = start
            chapters.append(Chapter(title=ln.strip(), level=1, start_char=start, end_char=-1))
    if chapters:
        chapters[-1].end_char = len(text)
    return ExtractResult(title=title, source_format="txt", text=text, chapters=chapters)


def _offset(lines: list[str], upto_line: int) -> int:
    """第 upto_line 行（含）之前文本的字符偏移。"""
    return sum(len(ln) + 1 for ln in lines[:upto_line])
