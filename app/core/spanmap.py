"""原文到删减稿的显式区间映射与一致性校验。"""
from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol

from ..models.domain import SpanMapEntry, SpanMapReport, SpanMapStatus


class RegionLike(Protocol):
    start: int
    end: int
    reason: str


def build_span_map(
    source_length: int, regions: Iterable[RegionLike]
) -> list[SpanMapEntry]:
    if source_length < 0:
        raise ValueError("source_length cannot be negative")
    ordered = sorted(regions, key=lambda region: (region.start, region.end))
    mapping: list[SpanMapEntry] = []
    source_cursor = 0
    target_cursor = 0
    for region in ordered:
        if region.start < source_cursor or region.end <= region.start:
            raise ValueError("deletion regions overlap or are empty")
        if region.end > source_length:
            raise ValueError("deletion region exceeds source length")
        if region.start > source_cursor:
            kept_length = region.start - source_cursor
            mapping.append(
                SpanMapEntry(
                    source_start=source_cursor,
                    source_end=region.start,
                    target_start=target_cursor,
                    target_end=target_cursor + kept_length,
                    status=SpanMapStatus.KEPT,
                )
            )
            target_cursor += kept_length
        mapping.append(
            SpanMapEntry(
                source_start=region.start,
                source_end=region.end,
                status=SpanMapStatus.DELETED,
                reason=region.reason,
            )
        )
        source_cursor = region.end
    if source_cursor < source_length:
        kept_length = source_length - source_cursor
        mapping.append(
            SpanMapEntry(
                source_start=source_cursor,
                source_end=source_length,
                target_start=target_cursor,
                target_end=target_cursor + kept_length,
                status=SpanMapStatus.KEPT,
            )
        )
    return mapping


def validate_span_map(
    source_text: str,
    target_text: str,
    mapping: list[SpanMapEntry],
) -> SpanMapReport:
    issues: list[str] = []
    source_cursor = 0
    target_cursor = 0
    source_covered = 0
    target_covered = 0

    for index, entry in enumerate(mapping):
        if entry.source_start != source_cursor:
            issues.append(
                f"source mapping gap or overlap before entry {index}: "
                f"expected {source_cursor}, got {entry.source_start}"
            )
        source_cursor = max(source_cursor, entry.source_end)
        source_covered += entry.source_length
        if entry.status == SpanMapStatus.DELETED:
            continue
        if entry.target_start != target_cursor:
            issues.append(
                f"target mapping gap or overlap before entry {index}: "
                f"expected {target_cursor}, got {entry.target_start}"
            )
        assert entry.target_start is not None and entry.target_end is not None
        source_slice = source_text[entry.source_start : entry.source_end]
        target_slice = target_text[entry.target_start : entry.target_end]
        if source_slice != target_slice:
            issues.append(f"source and target text differ at entry {index}")
        target_cursor = max(target_cursor, entry.target_end)
        target_covered += entry.target_length

    if source_cursor != len(source_text):
        issues.append(
            f"source mapping ends at {source_cursor}, expected {len(source_text)}"
        )
    if target_cursor != len(target_text):
        issues.append(
            f"target mapping ends at {target_cursor}, expected {len(target_text)}"
        )
    source_coverage = 1.0 if not source_text else min(1.0, source_covered / len(source_text))
    target_coverage = 1.0 if not target_text else min(1.0, target_covered / len(target_text))
    return SpanMapReport(
        valid=not issues,
        source_coverage=round(source_coverage, 6),
        target_coverage=round(target_coverage, 6),
        issues=issues,
    )

