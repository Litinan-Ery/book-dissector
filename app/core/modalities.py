"""在标准化正文中建立内容模态清单。"""
from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from typing import TYPE_CHECKING

from .runs import make_source_id
from ..models.domain import ContentBlock, Modality, SourceSpan

if TYPE_CHECKING:
    from .extractors.base import Chapter


def _block_id(
    source_fingerprint: str, modality: Modality, start: int, end: int
) -> str:
    raw = f"{source_fingerprint}:{modality.value}:{start}:{end}".encode("utf-8")
    return "B" + hashlib.sha256(raw).hexdigest()[:16].upper()


def _make_block(
    *,
    text: str,
    source_fingerprint: str,
    modality: Modality,
    start: int,
    end: int,
    warning: str = "",
) -> ContentBlock:
    return ContentBlock(
        block_id=_block_id(source_fingerprint, modality, start, end),
        modality=modality,
        source_span=SourceSpan(
            source_id=make_source_id(source_fingerprint, start, end),
            start_char=start,
            end_char=end,
        ),
        text=text[start:end],
        parse_warning=warning,
    )


def inventory_content(
    text: str,
    source_fingerprint: str,
    source_format: str,
    chapters: Iterable[Chapter] | None = None,
) -> list[ContentBlock]:
    """识别正文、标题、表格、图片、公式、代码和脚注。

    当前仅可靠保留表格/公式的文本表示，不能保证理解其视觉结构或语义；
    因此这些模态与图片都会产生显式告警。
    """
    if not text:
        return []

    occurrences: list[tuple[Modality, int, int, str]] = [
        (Modality.TEXT, 0, len(text), "")
    ]

    if source_format.lower() == "md":
        for match in re.finditer(r"(?m)^#{1,6}[ \t]+.+$", text):
            occurrences.append((Modality.HEADING, match.start(), match.end(), ""))
    else:
        for match in re.finditer(
            r"(?m)^\s*(?:第\s*[0-9一二三四五六七八九十百千零〇]+\s*[章回节部卷]|"
            r"Chapter\s+\d+|CHAPTER\s+\d+).*$",
            text,
        ):
            occurrences.append((Modality.HEADING, match.start(), match.end(), ""))

    for chapter in chapters or []:
        start = max(0, min(chapter.start_char, len(text) - 1))
        end = min(len(text), start + max(1, len(chapter.title)))
        if end > start:
            occurrences.append((Modality.HEADING, start, end, ""))

    patterns: list[tuple[Modality, re.Pattern[str], str]] = [
        (
            Modality.TABLE,
            re.compile(r"(?m)^(?:\s*\|.*\|\s*(?:\n|$)){2,}"),
            "表格文本已保留，但单元格合并和视觉关系尚未可靠解析",
        ),
        (
            Modality.TABLE,
            re.compile(r"(?ms)^\[表格(?:：[^\]]+)?\]\s*\n(?:\|.*\|\s*(?:\n|$))+"),
            "表格文本已保留，但单元格合并和视觉关系尚未可靠解析",
        ),
        (
            Modality.IMAGE,
            re.compile(r"!\[[^\]]*\]\([^)]+\)|\[图片(?:：[^\]]*)?\]"),
            "图片位置已保留，但图片语义尚未解析",
        ),
        (
            Modality.FORMULA,
            re.compile(r"\$\$.*?\$\$|(?<!\$)\$[^$\n]+\$(?!\$)|\[公式(?:：[^\]]*)?\]", re.S),
            "公式文本已保留，但公式结构和含义尚未可靠校验",
        ),
        (
            Modality.CODE,
            re.compile(r"```[^\n]*\n.*?```", re.S),
            "",
        ),
        (
            Modality.FOOTNOTE,
            re.compile(r"(?m)^\[\^[^\]]+\]:.*$|^\[脚注\].*$"),
            "",
        ),
    ]
    for modality, pattern, warning in patterns:
        for match in pattern.finditer(text):
            if match.end() > match.start():
                occurrences.append((modality, match.start(), match.end(), warning))

    unique: dict[tuple[Modality, int, int], tuple[Modality, int, int, str]] = {}
    for occurrence in occurrences:
        unique[(occurrence[0], occurrence[1], occurrence[2])] = occurrence

    return [
        _make_block(
            text=text,
            source_fingerprint=source_fingerprint,
            modality=modality,
            start=start,
            end=end,
            warning=warning,
        )
        for modality, start, end, warning in sorted(
            unique.values(), key=lambda item: (item[1], item[2], item[0].value)
        )
    ]
