"""EPUB 文本提取：使用 ebooklib 解析，HTML 转纯文本，h1-h6 作为章节。"""
from __future__ import annotations

import html
import re
from pathlib import Path

from ebooklib import ITEM_DOCUMENT, epub

from .base import Chapter, ExtractResult

_TAG_RE = re.compile(r"<[^>]+>")


def _plain(fragment: str) -> str:
    return html.unescape(_TAG_RE.sub("", fragment)).strip()


def _preserve_modalities(raw: str) -> str:
    """把会被去标签过程吞掉的模态变成显式文本标记。"""
    raw = re.sub(
        r"<pre[^>]*>(.*?)</pre>",
        lambda match: f"\n```\n{_plain(match.group(1))}\n```\n",
        raw,
        flags=re.I | re.S,
    )

    def table_replacement(match: re.Match[str]) -> str:
        rows: list[str] = []
        for row in re.findall(r"<tr[^>]*>(.*?)</tr>", match.group(1), flags=re.I | re.S):
            cells = [
                _plain(cell)
                for cell in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row, flags=re.I | re.S)
            ]
            if cells:
                rows.append("| " + " | ".join(cells) + " |")
        rendered = "\n".join(rows) or _plain(match.group(1))
        return f"\n[表格]\n{rendered}\n"

    raw = re.sub(
        r"<table[^>]*>(.*?)</table>", table_replacement, raw, flags=re.I | re.S
    )
    raw = re.sub(
        r"<math[^>]*>(.*?)</math>",
        lambda match: f"\n[公式：{_plain(match.group(1))}]\n",
        raw,
        flags=re.I | re.S,
    )
    raw = re.sub(
        r"<(?:aside|span)[^>]*(?:epub:type|role)=[\"'][^\"']*(?:footnote|doc-footnote)[^\"']*[\"'][^>]*>(.*?)</(?:aside|span)>",
        lambda match: f"\n[脚注] {_plain(match.group(1))}\n",
        raw,
        flags=re.I | re.S,
    )

    def image_replacement(match: re.Match[str]) -> str:
        tag = match.group(0)
        alt_match = re.search(r"\balt=[\"']([^\"']*)[\"']", tag, flags=re.I)
        alt = html.unescape(alt_match.group(1)).strip() if alt_match else ""
        return f"\n[图片：{alt or '无替代文本'}]\n"

    return re.sub(r"<img\b[^>]*>", image_replacement, raw, flags=re.I)


def _clean_html(raw: str) -> str:
    """去标签 + 去实体 + 折叠空行，保留段落换行。"""
    raw = _preserve_modalities(raw)
    # 块级标签后补换行，避免段落粘连
    raw = re.sub(r"</(p|div|h[1-6]|li|blockquote|tr)>", "\n", raw, flags=re.I)
    raw = re.sub(r"<(br|hr)[^>]*>", "\n", raw, flags=re.I)
    text = _TAG_RE.sub("", raw)
    text = html.unescape(text)
    lines = [ln.strip() for ln in text.splitlines()]
    out: list[str] = []
    blank = False
    for ln in lines:
        if not ln:
            blank = True
            continue
        if blank and out:
            out.append("")
        out.append(ln)
        blank = False
    return "\n".join(out)


def extract(path: Path) -> ExtractResult:
    try:
        book = epub.read_epub(str(path))
    except Exception as exc:
        return ExtractResult(error=f"EPUB 解析失败：{exc.__class__.__name__}")

    titles = book.get_metadata("DC", "title")
    creators = book.get_metadata("DC", "creator")
    title = titles[0][0] if titles else ""
    author = creators[0][0] if creators else ""

    chapters: list[Chapter] = []
    parts: list[str] = []

    # 仅按 EPUB spine（阅读顺序）提取正文，导航页虽然也是 XHTML，
    # 但不能被当作正文或章节重复读入。
    documents = {
        item.get_id(): item
        for item in book.get_items_of_type(ITEM_DOCUMENT)
        if not isinstance(item, epub.EpubNav)
    }
    ordered_items = [
        documents[item_id]
        for item_id, _linear in book.spine
        if item_id in documents
    ]
    if not ordered_items:
        ordered_items = list(documents.values())

    for item in ordered_items:
        raw = item.get_content().decode("utf-8", errors="replace")
        text = _clean_html(raw)
        if not text.strip():
            continue
        document_start = sum(len(part) for part in parts) + 2 * len(parts)
        search_from = 0
        # 找该文档中的标题
        for m in re.finditer(
            r"<h([1-6])[^>]*>(.*?)</h\1>", raw, flags=re.I | re.S
        ):
            level = int(m.group(1))
            htitle = html.unescape(_TAG_RE.sub("", m.group(2))).strip()
            if htitle:
                # 标题偏移必须落在清洗后的正文中；同一 XHTML 中的多个
                # 标题按出现顺序逐个定位，不能都指向文档起点。
                local_start = text.find(htitle, search_from)
                if local_start < 0:
                    continue
                chapters.append(
                    Chapter(
                        title=htitle,
                        level=level,
                        start_char=document_start + local_start,
                        end_char=-1,  # 结束时统一修正
                    )
                )
                search_from = local_start + len(htitle)
        parts.append(text)

    full = "\n\n".join(parts)
    # 修正章节区间
    for i, ch in enumerate(chapters):
        end = chapters[i + 1].start_char if i + 1 < len(chapters) else len(full)
        ch.end_char = end
    return ExtractResult(
        title=title,
        author=author,
        source_format="epub",
        text=full,
        chapters=chapters,
    )
