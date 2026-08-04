"""汇总 P0 指标并决定正式结果是否可发布。"""
from __future__ import annotations

from ..models.domain import (
    KnowledgeKind,
    KnowledgeUnit,
    QualityReport,
    QualityStatus,
    SpanMapReport,
    StructureReport,
    UnitCoverageReport,
    VerificationStatus,
)
from .dedup import find_duplicate_pairs


def evaluate_quality(
    *,
    structure: StructureReport,
    span_map: SpanMapReport,
    unit_coverage: UnitCoverageReport,
    knowledge_units: list[KnowledgeUnit],
    modality_warnings: list[str],
    processing_errors: list[str],
    duplicate_merged_count: int,
) -> QualityReport:
    blockers: list[str] = []
    warnings: list[str] = []
    body_coverage = min(structure.body_coverage, unit_coverage.coverage)

    if not structure.valid:
        blockers.append("章节结构校验失败")
    if not span_map.valid or span_map.source_coverage < 1.0 or span_map.target_coverage < 1.0:
        blockers.append("原文到删减稿的映射不完整或不一致")
    if body_coverage < 0.99:
        blockers.append(f"核心正文覆盖率 {body_coverage:.2%} 低于 99%")

    core = [unit for unit in knowledge_units if unit.kind != KnowledgeKind.UNKNOWN]
    verified = [
        unit
        for unit in core
        if unit.verification_status == VerificationStatus.VERIFIED and unit.anchors
    ]
    anchor_coverage = len(verified) / len(core) if core else 0.0
    if not core:
        blockers.append("没有生成可核验的核心知识单元")
    elif anchor_coverage < 1.0:
        blockers.append(f"核心知识锚点覆盖率 {anchor_coverage:.2%} 低于 100%")

    duplicates = find_duplicate_pairs(knowledge_units)
    if duplicates:
        blockers.append(f"仍有 {len(duplicates)} 组重复知识未合并")
    if modality_warnings:
        blockers.extend(f"未解析模态：{warning}" for warning in modality_warnings)
    if processing_errors:
        blockers.extend(f"处理错误：{error}" for error in processing_errors)
    unknown_count = sum(unit.kind == KnowledgeKind.UNKNOWN for unit in knowledge_units)
    if unknown_count:
        warnings.append(f"{unknown_count} 条未知内容未进入正式正文")

    status = QualityStatus.FAIL if blockers else (
        QualityStatus.WARNING if warnings else QualityStatus.PASS
    )
    return QualityReport(
        status=status,
        body_coverage=round(body_coverage, 6),
        anchor_coverage=round(anchor_coverage, 6),
        duplicate_merged_count=duplicate_merged_count,
        warnings=warnings,
        blocking_issues=blockers,
    )

