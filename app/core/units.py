"""建立覆盖完整、可回到原文的提炼单元。"""
from __future__ import annotations

import hashlib
from collections.abc import Iterable

from .extractors.base import Chapter
from .runs import make_source_id
from ..models.domain import (
    CharacterRange,
    DistillUnit,
    SourceSpan,
    SpanMapEntry,
    SpanMapStatus,
    UnitCoverageReport,
)


def source_spans_for_target(
    target_start: int,
    target_end: int,
    span_map: Iterable[SpanMapEntry],
    source_fingerprint: str,
) -> list[SourceSpan]:
    if target_start < 0 or target_end <= target_start:
        raise ValueError("target range must be non-empty")
    spans: list[SourceSpan] = []
    for entry in span_map:
        if entry.status != SpanMapStatus.KEPT:
            continue
        assert entry.target_start is not None and entry.target_end is not None
        overlap_start = max(target_start, entry.target_start)
        overlap_end = min(target_end, entry.target_end)
        if overlap_end <= overlap_start:
            continue
        source_start = entry.source_start + (overlap_start - entry.target_start)
        source_end = source_start + (overlap_end - overlap_start)
        spans.append(
            SourceSpan(
                source_id=make_source_id(source_fingerprint, source_start, source_end),
                start_char=source_start,
                end_char=source_end,
            )
        )
    if not spans:
        raise ValueError("target range is not covered by kept source spans")
    return spans


def _split_range(
    text: str,
    start: int,
    end: int,
    *,
    max_chars: int,
    overlap_chars: int,
) -> list[tuple[int, int]]:
    if end - start <= max_chars:
        return [(start, end)]
    ranges: list[tuple[int, int]] = []
    cursor = start
    while cursor < end:
        boundary = min(cursor + max_chars, end)
        if boundary < end:
            search_start = cursor + max(1, max_chars // 2)
            paragraph = text.rfind("\n\n", search_start, boundary)
            line = text.rfind("\n", search_start, boundary)
            natural = max(paragraph + 2 if paragraph >= 0 else -1, line + 1 if line >= 0 else -1)
            if natural > cursor:
                boundary = natural
        ranges.append((cursor, boundary))
        if boundary >= end:
            break
        next_cursor = boundary - overlap_chars
        if next_cursor <= cursor:
            next_cursor = boundary
        cursor = next_cursor
    return ranges


def build_distill_units(
    *,
    text: str,
    chapters: list[Chapter],
    span_map: list[SpanMapEntry],
    source_fingerprint: str,
    max_chars: int,
    overlap_chars: int,
    target_ratio: float,
) -> list[DistillUnit]:
    if not text:
        return []
    if max_chars <= 0 or overlap_chars < 0 or overlap_chars >= max_chars:
        raise ValueError("invalid chunk size or overlap")
    if target_ratio <= 0:
        raise ValueError("target_ratio must be positive")

    sections = [
        (chapter.title or f"章节 {index + 1}", chapter.start_char, chapter.end_char)
        for index, chapter in enumerate(chapters)
        if 0 <= chapter.start_char < chapter.end_char <= len(text)
    ]
    if not sections:
        sections = [("全文", 0, len(text))]

    units: list[DistillUnit] = []
    for title, section_start, section_end in sections:
        ranges = _split_range(
            text,
            section_start,
            section_end,
            max_chars=max_chars,
            overlap_chars=overlap_chars,
        )
        for index, (start, end) in enumerate(ranges, start=1):
            input_text = text[start:end]
            unit_title = title if len(ranges) == 1 else f"{title}（{index}/{len(ranges)}）"
            identity = hashlib.sha256(
                f"{source_fingerprint}:{start}:{end}:{title}".encode("utf-8")
            ).hexdigest()
            units.append(
                DistillUnit(
                    unit_id="U" + identity[:16].upper(),
                    title=title,
                    target_start=start,
                    target_end=end,
                    input_text=input_text,
                    source_spans=source_spans_for_target(
                        start, end, span_map, source_fingerprint
                    ),
                    input_fingerprint=hashlib.sha256(input_text.encode("utf-8")).hexdigest(),
                    target_chars=max(1, int(len(input_text) * target_ratio)),
                )
            )
    return units


def validate_unit_coverage(
    units: list[DistillUnit], *, body_start: int, body_end: int
) -> UnitCoverageReport:
    if body_start < 0 or body_end <= body_start:
        raise ValueError("body range must be non-empty")
    intervals = sorted(
        (
            max(body_start, unit.target_start),
            min(body_end, unit.target_end),
        )
        for unit in units
        if unit.target_end > body_start and unit.target_start < body_end
    )
    uncovered: list[CharacterRange] = []
    duplicates: list[CharacterRange] = []
    cursor = body_start
    covered = 0
    for start, end in intervals:
        if end <= start:
            continue
        if start > cursor:
            uncovered.append(CharacterRange(start=cursor, end=start))
        if start < cursor:
            duplicate_end = min(cursor, end)
            if duplicate_end > start:
                duplicates.append(CharacterRange(start=start, end=duplicate_end))
        new_start = max(start, cursor)
        if end > new_start:
            covered += end - new_start
        cursor = max(cursor, end)
    if cursor < body_end:
        uncovered.append(CharacterRange(start=cursor, end=body_end))
    coverage = covered / (body_end - body_start)
    return UnitCoverageReport(
        coverage=round(coverage, 6),
        uncovered_ranges=uncovered,
        duplicate_ranges=duplicates,
    )

