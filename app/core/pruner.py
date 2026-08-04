"""无关内容识别与删减（本地规则实现，不调用大模型）。

识别并剔除：
- 元信息 frontmatter（YAML 键值块，MD 来源）
- 版权页 / 出版信息（正文起点之前，含版权关键词的块）
- 目录页（正文起点之前，含目录标题的块）
- 尾注 / 参考文献 / 索引（书末部分标题之后的区域）
- 重复段（页眉/页脚/重复广告）

保留：序言 / 导言 / 后记（FR-2.5 默认保留）。

用户可通过 restore_regions 恢复误删区域（FR-2.6）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .extractors.base import Chapter
from .spanmap import build_span_map, validate_span_map
from ..models.domain import SpanMapEntry, SpanMapReport

# ---- 关键词模式（匹配"单行"标题；行首 Markdown # 前缀由 _line 统一剥离）----
_COPYRIGHT_PAT = re.compile(
    r"版权|版权所有|ISBN|Copyright|All rights reserved|出版发行|印刷|"
    r"版次|CIP|翻印必究|定价[:：]",
    re.I,
)
_TOC_PAT = re.compile(r"^(目录|目\s*录|CONTENTS|Table of Contents)$", re.I)
_BACKMATTER_PAT = re.compile(
    r"^(参考文献|参考书目|尾注|索引|附注|致谢|注释|注释和参考文献|"
    r"注释与参考文献|注释及参考文献|注释?参考文献|"
    r"References|Bibliography|Endnotes|Index|Notes|Notes and References|"
    r"Notes & References)$",
    re.I,
)
_STRUCT_HEADING_PAT = re.compile(
    r"^(第\s*[0-9一二三四五六七八九十百千零〇]+\s*[章回节部卷]|"
    r"Chapter\s+\d+|CHAPTER\s+\d+|序言|序|前言|自序|导言|引言|导读|"
    r"Preface|Introduction|Foreword|后记|结语|跋|Afterword|Epilogue|"
    r"Conclusion)\b"
)
_MAIN_TITLE_PAT = re.compile(
    r"^(第\s*[0-9一二三四五六七八九十百千零〇]+\s*[章回节部卷]|"
    r"Chapter\s+\d+|CHAPTER\s+\d+)\b"
)
# 目录条目：标题 + 点线/空白 + 页码
_TOC_ENTRY_PAT = re.compile(r"^.{1,60}[.．·•\s]{2,}\d{1,4}\s*$")
# frontmatter：--- 包裹的键值行
_FRONTMATTER_PAT = re.compile(r"^---\s*$")
_FRONTMATTER_KEY_PAT = re.compile(r"^[a-zA-Z_][\w-]*\s*:")


@dataclass
class Region:
    """一个删除区域（基于原始全文的字符区间）。"""

    start: int
    end: int
    reason: str  # frontmatter / copyright / toc / backmatter / duplicate
    label: str = ""

    def __len__(self) -> int:
        return max(0, self.end - self.start)


@dataclass
class PruneResult:
    """删减结果。"""

    pruned_text: str
    regions: list[Region] = field(default_factory=list)
    original_chars: int = 0
    removed_chars: int = 0
    pruned_chapters: list[Chapter] = field(default_factory=list)
    evidence_regions: list[Region] = field(default_factory=list)
    span_map: list[SpanMapEntry] = field(default_factory=list)
    span_map_report: SpanMapReport = field(
        default_factory=lambda: SpanMapReport(
            valid=False, source_coverage=0.0, target_coverage=0.0
        )
    )

    @property
    def kept_ratio(self) -> float:
        if self.original_chars <= 0:
            return 0.0
        return round((self.original_chars - self.removed_chars) / self.original_chars, 4)


def _line(text: str, start: int, end: int) -> str:
    """行内容，剥离行首 Markdown 标题符号。"""
    s = text[start:end].strip()
    return re.sub(r"^#+\s*", "", s).strip()


def _summarize(text: str, start: int, end: int, limit: int = 30) -> str:
    snippet = re.sub(r"\s+", " ", text[start:end].strip())
    return snippet[:limit] + ("…" if len(snippet) > limit else "")


def _line_spans(text: str) -> list[tuple[int, int]]:
    """所有行的 (start, end) 区间（含行尾换行）。"""
    spans: list[tuple[int, int]] = []
    pos = 0
    for m in re.finditer(r"[^\n]*\n?", text):
        spans.append((m.start(), m.end()))
    return spans


def _merge_regions(regions: list[Region]) -> list[Region]:
    if not regions:
        return []
    ordered = sorted(regions, key=lambda r: (r.start, r.end))
    merged: list[Region] = []
    cur = ordered[0]
    for r in ordered[1:]:
        if r.start <= cur.end:
            if r.end > cur.end:
                cur.end = r.end
                if r.label and len(r.label) > len(cur.label):
                    cur.label = r.label
        else:
            merged.append(cur)
            cur = r
    merged.append(cur)
    return merged


def _locate_main_start(text: str, chapters: list[Chapter]) -> int | None:
    """正文起点：第一个 level<=2 章节标题的位置；无章节结构时用关键词兜底。"""
    for ch in chapters:
        if ch.level <= 2 and ch.start_char > 0:
            return ch.start_char
    for s, e in _line_spans(text):
        if _MAIN_TITLE_PAT.match(_line(text, s, e)):
            return s
    return None


def _find_heading_positions(text: str, pat: re.Pattern) -> list[tuple[int, str]]:
    """返回匹配标题的行起始偏移与（去 # 后的）标题文本。"""
    out: list[tuple[int, str]] = []
    for s, e in _line_spans(text):
        line = _line(text, s, e)
        if line and pat.match(line):
            out.append((s, line))
    return out


def _split_blocks(text: str) -> list[tuple[int, int]]:
    """按空行切块，返回每块的 (start, end)。"""
    blocks: list[tuple[int, int]] = []
    start = None
    for m in re.finditer(r"^[ \t]*\n", text, flags=re.M):
        if start is None:
            start = 0
        blocks.append((start, m.start()))
        start = m.end()
    if start is None:
        return [(0, len(text))] if text.strip() else []
    blocks.append((start, len(text)))
    return [(s, e) for s, e in blocks if text[s:e].strip()]


def _is_frontmatter(text: str, start: int, end: int) -> bool:
    """块是否为 YAML frontmatter（--- 开头，含键值行）。"""
    lines = [ln for ln in text[start:end].splitlines() if ln.strip()]
    if len(lines) < 2:
        return False
    if not _FRONTMATTER_PAT.match(lines[0].strip()):
        return False
    if not _FRONTMATTER_PAT.match(lines[-1].strip()):
        return False
    body = lines[1:-1]
    if not body:
        return False
    key_lines = sum(1 for ln in body if _FRONTMATTER_KEY_PAT.match(ln.strip()))
    return key_lines >= max(1, len(body))


def _count_toc_entries(text: str, start: int, end: int) -> int:
    n = 0
    for ln in text[start:end].splitlines():
        if _TOC_ENTRY_PAT.match(ln.strip()):
            n += 1
    return n


def prune(
    text: str,
    chapters: list[Chapter] | None = None,
    restore_regions: list[tuple[int, int]] | None = None,
) -> PruneResult:
    """执行删减。restore_regions 为需要恢复保留的原始区间（FR-2.6）。"""
    chapters = chapters or []
    text_len = len(text)
    regions: list[Region] = []
    evidence_regions: list[Region] = []
    line_spans = _line_spans(text)

    # ---- 0. frontmatter（MD 元信息块）----
    for s, e in _split_blocks(text):
        if s == 0 and _is_frontmatter(text, s, e):
            regions.append(Region(s, e, "frontmatter", _summarize(text, s, e)))
            break

    main_start = _locate_main_start(text, chapters)

    # ---- 1. 正文起点之前的头部区域：版权 / 目录 / 封面 ----
    head_end = main_start if main_start is not None else min(text_len, 5000)
    head_blocks = [b for b in _split_blocks(text[:head_end]) if b[0] < head_end]

    # 目录标题行（可能单独成块，也可能与条目同块）
    toc_heading_positions = [
        s for s, _ in _find_heading_positions(text[:head_end], _TOC_PAT)
    ]
    toc_ranges: list[tuple[int, int]] = []
    for pos in toc_heading_positions:
        toc_ranges.append((pos, head_end))
        # 目录通常在正文起点前结束；若后续块不含条目，收窄到标题所在块的末尾
        for bs, be in head_blocks:
            if bs <= pos < be:
                if _count_toc_entries(text, be, head_end) < 4:
                    toc_ranges[-1] = (pos, be)
                break

    for bstart, bend in head_blocks:
        block = text[bstart:bend]
        if _COPYRIGHT_PAT.search(block):
            regions.append(Region(bstart, bend, "copyright", _summarize(text, bstart, bend)))
            continue
        if any(rs <= bstart < re_ for rs, re_ in toc_ranges):
            regions.append(Region(bstart, bend, "toc", _summarize(text, bstart, bend)))
            continue
        # 目录条目密集块（无"目录"标题时）
        if len(block) < 3000 and _count_toc_entries(text, bstart, bend) >= 4:
            regions.append(Region(bstart, bend, "toc", _summarize(text, bstart, bend)))

    # ---- 2. 书末部分：保留为证据区，不再按类型一刀切删除 ----
    for pos, heading in _find_heading_positions(text, _BACKMATTER_PAT):
        if main_start is not None and pos < main_start:
            continue
        end_pos = _next_structural_boundary(text, chapters, pos + len(heading))
        evidence_regions.append(Region(pos, end_pos, "backmatter_evidence", heading))

    # ---- 3. 重复段：非空长行出现 >=3 次 ----
    dup_lines: dict[str, int] = {}
    for s, e in line_spans:
        ln = text[s:e].strip()
        if len(ln) >= 12:
            dup_lines[ln] = dup_lines.get(ln, 0) + 1
    for s, cnt in dup_lines.items():
        if cnt >= 3:
            # 第一处是知识来源，只有后续复本可删除。
            for m in list(re.finditer(re.escape(s), text))[1:]:
                regions.append(Region(m.start(), m.end(), "duplicate", s[:30]))

    # ---- 4. 恢复用户指定区域 ----
    if restore_regions:
        regions = _exclude_regions(regions, restore_regions)

    regions = _merge_regions(regions)

    # ---- 5. 生成删减稿 ----
    removed = 0
    parts: list[str] = []
    cursor = 0
    for r in regions:
        if r.start > cursor:
            parts.append(text[cursor : r.start])
        removed += len(r)
        cursor = max(cursor, r.end)
    parts.append(text[cursor:])
    pruned = "".join(parts)

    # 计算删减稿中的章节新偏移（供后续阶段使用，避免偏移错位）
    pruned_chapters = _remap_chapters(chapters, regions, text_len, pruned)
    span_map = build_span_map(text_len, regions)
    span_map_report = validate_span_map(text, pruned, span_map)

    return PruneResult(
        pruned_text=pruned,
        regions=regions,
        original_chars=text_len,
        removed_chars=removed,
        pruned_chapters=pruned_chapters,
        evidence_regions=evidence_regions,
        span_map=span_map,
        span_map_report=span_map_report,
    )


def _next_structural_boundary(
    text: str, chapters: list[Chapter], from_pos: int
) -> int:
    """backmatter 区域的终点：下一个章节标题偏移；无则到文末。"""
    candidates = [c.start_char for c in chapters if c.start_char > from_pos]
    if candidates:
        return min(candidates)
    for s, e in _line_spans(text):
        if s >= from_pos and _STRUCT_HEADING_PAT.match(_line(text, s, e)):
            return s
    return len(text)


def _remap_chapters(
    chapters: list[Chapter], regions: list[Region], text_len: int, pruned: str
) -> list[Chapter]:
    """把章节偏移映射到删减稿：新 start = 原 start - 其前方被删字符数。

    章节内容被整体删除（起点落在删除区域内，或与删除区域重叠 >=50%）
    时，在删减稿中不再保留该章节。
    """
    regions = sorted(regions, key=lambda r: r.start)
    out: list[Chapter] = []

    def map_offset(position: int) -> int:
        """把原文偏移映射到删减稿，包含 position 之前的章内删除。"""
        clipped = min(max(position, 0), text_len)
        removed = sum(
            max(0, min(region.end, clipped) - region.start)
            for region in regions
            if region.start < clipped
        )
        return clipped - removed

    for ch in chapters:
        original_start = min(max(ch.start_char, 0), text_len)
        original_end = min(max(ch.end_char, original_start), text_len)
        ch_len = max(1, original_end - original_start)
        # 起点落在删除区域内：整体删除
        if any(r.start <= original_start < r.end for r in regions):
            continue
        # 与删除区域重叠 >=50%（如"致谢"章节被 backmatter 规则大部分删除）
        overlap = sum(
            max(0, min(r.end, original_end) - max(r.start, original_start))
            for r in regions
            if r.start < original_end and r.end > original_start
        )
        if overlap >= ch_len * 0.5:
            continue
        new_start = map_offset(original_start)
        new_end = map_offset(original_end)
        if new_start < len(pruned) and new_end > new_start:
            out.append(
                Chapter(
                    title=ch.title,
                    level=ch.level,
                    start_char=new_start,
                    end_char=min(new_end, len(pruned)),
                )
            )
    return out


def _exclude_regions(
    regions: list[Region], restore: list[tuple[int, int]]
) -> list[Region]:
    """从删除区域中减去恢复区间。"""
    out: list[Region] = []
    for r in regions:
        cut = [r]
        for rs, re_ in restore:
            new_cut: list[Region] = []
            for c in cut:
                if re_ <= c.start or rs >= c.end:
                    new_cut.append(c)
                    continue
                if rs > c.start:
                    new_cut.append(Region(c.start, rs, c.reason, c.label))
                if re_ < c.end:
                    new_cut.append(Region(re_, c.end, c.reason, c.label))
            cut = new_cut
        out.extend(cut)
    return out
