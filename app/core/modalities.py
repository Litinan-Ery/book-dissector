"""轻量内容类型识别：只发现并告警，不引入质量门禁。"""
from __future__ import annotations

import re
from typing import Any

_TEXT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("table", re.compile(r"(?m)^(?:\s*\|.*\|\s*(?:\n|$)){2,}")),
    ("image", re.compile(r"!\[[^\]]*\]\([^)]+\)|\[图片(?:：[^\]]*)?\]")),
    ("formula", re.compile(r"\$\$.*?\$\$|(?<!\$)\$[^$\n]+\$(?!\$)|\[公式(?:：[^\]]*)?\]", re.S)),
    ("code", re.compile(r"```[^\n]*\n.*?```", re.S)),
)

_HTML_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("table", re.compile(r"<table\b", re.I)),
    ("image", re.compile(r"<(?:img|svg)\b", re.I)),
    ("formula", re.compile(r"<(?:math|m:math)\b|class=[\"'][^\"']*(?:math|latex)", re.I)),
    ("code", re.compile(r"<(?:pre|code)\b", re.I)),
)

_MESSAGES = {
    "table": "检测到表格；当前不能可靠保留单元格合并和视觉关系",
    "image": "检测到图片；当前不能理解图片语义",
    "formula": "检测到公式；当前不能可靠校验公式结构和含义",
    "code": "检测到代码块；代码文本会保留，但排版和运行结果未验证",
}


def detect_text_modalities(text: str, *, location: str = "正文") -> list[dict[str, Any]]:
    warnings: list[dict[str, Any]] = []
    for kind, pattern in _TEXT_PATTERNS:
        matches = list(pattern.finditer(text))
        if matches:
            warnings.append(
                {
                    "type": kind,
                    "count": len(matches),
                    "location": location,
                    "message": _MESSAGES[kind],
                }
            )
    return warnings


def detect_html_modalities(raw_html: str, *, location: str) -> list[dict[str, Any]]:
    warnings: list[dict[str, Any]] = []
    for kind, pattern in _HTML_PATTERNS:
        count = len(pattern.findall(raw_html))
        if count:
            warnings.append(
                {
                    "type": kind,
                    "count": count,
                    "location": location,
                    "message": _MESSAGES[kind],
                }
            )
    return warnings


def merge_warnings(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按类型和位置合并重复识别结果。"""
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    for item in items:
        key = (str(item.get("type", "unknown")), str(item.get("location", "正文")))
        if key not in merged:
            merged[key] = dict(item)
        else:
            merged[key]["count"] = max(
                int(merged[key].get("count", 0)), int(item.get("count", 0))
            )
    return list(merged.values())
