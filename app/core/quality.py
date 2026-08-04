"""不依赖模型的 P0 质量门禁。"""
from __future__ import annotations

from typing import TYPE_CHECKING

from ..models.domain import CharacterRange, StructureIssue, StructureReport

if TYPE_CHECKING:
    from .extractors.base import Chapter


def _merge_ranges(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    if not ranges:
        return []
    merged: list[list[int]] = []
    for start, end in sorted(ranges):
        if not merged or start > merged[-1][1]:
            merged.append([start, end])
        else:
            merged[-1][1] = max(merged[-1][1], end)
    return [(start, end) for start, end in merged]


def validate_structure(
    text: str,
    chapters: list[Chapter],
    *,
    min_coverage: float = 0.99,
) -> StructureReport:
    """校验章节区间并计算核心正文并集覆盖率。

    显式章节存在时，首章之前视为结构性前置区域，不计入正文覆盖分母；
    首章起点到文末都必须被章节并集覆盖。无显式章节时，全文作为一个
    隐式提炼单元。
    """
    text_length = len(text)
    issues: list[StructureIssue] = []
    if text_length == 0:
        return StructureReport(
            valid=False,
            body_start=0,
            body_end=0,
            body_coverage=0.0,
            issues=[StructureIssue(code="empty_document", message="正文为空")],
        )

    if not chapters:
        return StructureReport(
            valid=True,
            body_start=0,
            body_end=text_length,
            body_coverage=1.0,
            issues=[
                StructureIssue(
                    code="implicit_document",
                    message="未发现显式章节，全文作为一个提炼单元",
                    blocking=False,
                )
            ],
        )

    intervals: list[tuple[int, int]] = []
    previous_start = -1
    for chapter in chapters:
        if chapter.start_char < previous_start:
            issues.append(
                StructureIssue(
                    code="out_of_order",
                    message="章节起点不是单调递增",
                    chapter_title=chapter.title,
                    start=chapter.start_char,
                    end=chapter.end_char,
                )
            )
        previous_start = chapter.start_char
        if chapter.end_char <= chapter.start_char:
            issues.append(
                StructureIssue(
                    code="empty_chapter",
                    message="章节区间为空",
                    chapter_title=chapter.title,
                    start=chapter.start_char,
                    end=chapter.end_char,
                )
            )
            continue
        if chapter.start_char < 0 or chapter.end_char > text_length:
            issues.append(
                StructureIssue(
                    code="out_of_bounds",
                    message="章节区间超出正文边界",
                    chapter_title=chapter.title,
                    start=chapter.start_char,
                    end=chapter.end_char,
                )
            )
        start = max(0, min(chapter.start_char, text_length))
        end = max(0, min(chapter.end_char, text_length))
        if end > start:
            intervals.append((start, end))

    if not intervals:
        return StructureReport(
            valid=False,
            body_start=0,
            body_end=text_length,
            body_coverage=0.0,
            uncovered_ranges=[CharacterRange(start=0, end=text_length)],
            issues=issues,
        )

    ordered = sorted(intervals)
    duplicate_ranges: list[tuple[int, int]] = []
    furthest_end = ordered[0][1]
    for start, end in ordered[1:]:
        if start < furthest_end:
            duplicate_ranges.append((start, min(end, furthest_end)))
        furthest_end = max(furthest_end, end)
    duplicate_ranges = _merge_ranges(duplicate_ranges)
    for start, end in duplicate_ranges:
        issues.append(
            StructureIssue(
                code="overlap",
                message="章节区间发生意外重叠",
                start=start,
                end=end,
            )
        )

    covered = _merge_ranges(ordered)
    body_start = covered[0][0]
    body_end = text_length
    uncovered: list[tuple[int, int]] = []
    cursor = body_start
    covered_chars = 0
    for start, end in covered:
        if start > cursor:
            uncovered.append((cursor, start))
        effective_start = max(start, cursor)
        if end > effective_start:
            covered_chars += end - effective_start
        cursor = max(cursor, end)
    if cursor < body_end:
        uncovered.append((cursor, body_end))

    body_chars = max(1, body_end - body_start)
    coverage = round(min(1.0, covered_chars / body_chars), 6)
    if coverage < min_coverage:
        issues.append(
            StructureIssue(
                code="coverage_below_threshold",
                message=f"正文覆盖率 {coverage:.2%} 低于 {min_coverage:.2%}",
            )
        )

    blocking = any(issue.blocking for issue in issues)
    return StructureReport(
        valid=not blocking,
        body_start=body_start,
        body_end=body_end,
        body_coverage=coverage,
        uncovered_ranges=[CharacterRange(start=start, end=end) for start, end in uncovered],
        duplicate_ranges=[
            CharacterRange(start=start, end=end) for start, end in duplicate_ranges
        ],
        issues=issues,
    )
