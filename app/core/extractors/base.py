"""提取器公共类型与分发入口。

extract_book(book_id, path) 是唯一对外接口：
- 按扩展名分发到各格式实现
- 提取纯文本、书名、作者、章节结构
- 结果写入 {book_id}.txt（全文）与 {book_id}.meta.json（元数据）
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from ... import config
from ..modalities import inventory_content
from ..quality import validate_structure
from ..runs import fingerprint_file

# 常见"第X章 / Chapter X"标题模式，用于无结构格式（TXT）的章节切分
CHAPTER_RE = re.compile(
    r"^\s*(第\s*[0-9一二三四五六七八九十百千零〇]+\s*[章回节部卷]|"
    r"Chapter\s+\d+|CHAPTER\s+\d+)\b"
)


@dataclass
class Chapter:
    """章节：标题 + 在全文中的字符区间。"""

    title: str
    level: int
    start_char: int
    end_char: int


@dataclass
class ExtractResult:
    """一次提取的结果。"""

    title: str = ""
    author: str = ""
    source_format: str = ""
    text: str = ""
    chapters: list[Chapter] = field(default_factory=list)
    error: str = ""

    @property
    def word_count(self) -> int:
        return len(re.sub(r"\s", "", self.text))

    @property
    def ok(self) -> bool:
        return not self.error and len(self.text.strip()) > 0


def _read_bytes(path: Path) -> bytes:
    return path.read_bytes()


def extract_book(book_id: str, path: Path) -> ExtractResult:
    """按扩展名分发提取，并持久化 .txt 与 .meta.json。"""
    ext = path.suffix.lower()
    if ext == ".epub":
        from . import epub as impl
    elif ext == ".pdf":
        from . import pdf as impl
    elif ext == ".txt":
        from . import txt as impl
    elif ext == ".md":
        from . import md as impl
    else:
        return ExtractResult(error=f"不支持的格式：{ext}")

    result = impl.extract(path)
    source_fingerprint = fingerprint_file(path)
    structure_report = validate_structure(result.text, result.chapters)
    content_blocks = inventory_content(
        result.text,
        source_fingerprint,
        result.source_format or ext.lstrip("."),
        result.chapters,
    )
    modality_warnings = sorted(
        {block.parse_warning for block in content_blocks if block.parse_warning}
    )

    config.ensure_dirs()
    txt_path = config.BOOKS_DIR / f"{book_id}.txt"
    meta_path = config.BOOKS_DIR / f"{book_id}.meta.json"

    if result.error or not result.text.strip():
        meta = {
            "book_id": book_id,
            "source_format": ext.lstrip("."),
            "title": result.title,
            "author": result.author,
            "word_count": 0,
            "chapters": [],
            "source_fingerprint": source_fingerprint,
            "structure_report": structure_report.model_dump(mode="json"),
            "content_blocks": [block.model_dump(mode="json") for block in content_blocks],
            "modality_warnings": modality_warnings,
            "extract_status": "error",
            "extract_error": result.error or "未能提取到文本（可能是扫描版 PDF）",
        }
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        if txt_path.exists():
            txt_path.unlink()
        return result

    txt_path.write_text(result.text, encoding="utf-8")
    meta = {
        "book_id": book_id,
        "source_format": ext.lstrip("."),
        "title": result.title or path.stem,
        "author": result.author,
        "word_count": result.word_count,
        "source_fingerprint": source_fingerprint,
        "chapters": [
            {"title": c.title, "level": c.level, "start_char": c.start_char, "end_char": c.end_char}
            for c in result.chapters
        ],
        "structure_report": structure_report.model_dump(mode="json"),
        "content_blocks": [block.model_dump(mode="json") for block in content_blocks],
        "modality_warnings": modality_warnings,
        "extract_status": "ok",
        "extract_error": "",
    }
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return result
