import json
from pathlib import Path

import pytest

from app import config
from app.core.dedup import merge_knowledge_units
from app.core.exporter import ExportQualityError, build_export_md
from app.core.quality_gate import evaluate_quality
from app.models.domain import (
    KnowledgeKind,
    KnowledgeUnit,
    QualityStatus,
    SourceAnchor,
    SpanMapReport,
    StructureReport,
    UnitCoverageReport,
    VerificationStatus,
)


def _anchor(source_id: str, start: int, quote: str) -> SourceAnchor:
    return SourceAnchor.from_text(
        source_id=source_id,
        start_char=start,
        end_char=start + len(quote),
        quote=quote,
    )


def _knowledge(
    knowledge_id: str,
    content: str,
    anchor: SourceAnchor,
    *,
    status: VerificationStatus = VerificationStatus.VERIFIED,
) -> KnowledgeUnit:
    return KnowledgeUnit(
        knowledge_id=knowledge_id,
        kind=KnowledgeKind.SOURCE_CLAIM,
        content=content,
        anchors=[anchor],
        verification_status=status,
    )


def test_full_book_dedup_merges_rephrasing_and_preserves_all_anchors() -> None:
    first = _knowledge(
        "K1",
        "作者指出：准确性是效率的前提。",
        _anchor("S1", 0, "准确性是效率的前提"),
    )
    second = _knowledge(
        "K2",
        "本章强调，准确性是效率的前提",
        _anchor("S2", 100, "先保证准确性，再追求效率"),
    )

    result = merge_knowledge_units([first, second])

    assert result.merged_count == 1
    assert len(result.units) == 1
    assert {anchor.source_id for anchor in result.units[0].anchors} == {"S1", "S2"}


def test_dedup_does_not_merge_opposite_claims() -> None:
    increase = _knowledge(
        "K1",
        "该机制会提高转化率",
        _anchor("S1", 0, "提高转化率"),
    )
    decrease = _knowledge(
        "K2",
        "该机制会降低转化率",
        _anchor("S2", 20, "降低转化率"),
    )

    result = merge_knowledge_units([increase, decrease])

    assert result.merged_count == 0
    assert len(result.units) == 2


def test_quality_gate_passes_only_with_complete_verified_evidence() -> None:
    knowledge = _knowledge(
        "K1",
        "准确性是效率的前提",
        _anchor("S1", 0, "准确性是效率的前提"),
    )

    report = evaluate_quality(
        structure=StructureReport(
            valid=True, body_start=0, body_end=100, body_coverage=1.0
        ),
        span_map=SpanMapReport(
            valid=True, source_coverage=1.0, target_coverage=1.0
        ),
        unit_coverage=UnitCoverageReport(coverage=1.0),
        knowledge_units=[knowledge],
        modality_warnings=[],
        processing_errors=[],
        duplicate_merged_count=0,
    )

    assert report.status == QualityStatus.PASS
    assert report.anchor_coverage == 1.0
    assert report.body_coverage == 1.0


def test_unverified_knowledge_or_unresolved_modality_blocks_formal_quality() -> None:
    unverified = _knowledge(
        "K1",
        "准确性是效率的前提",
        _anchor("S1", 0, "准确性是效率的前提"),
        status=VerificationStatus.UNVERIFIED,
    )

    report = evaluate_quality(
        structure=StructureReport(
            valid=True, body_start=0, body_end=100, body_coverage=1.0
        ),
        span_map=SpanMapReport(
            valid=True, source_coverage=1.0, target_coverage=1.0
        ),
        unit_coverage=UnitCoverageReport(coverage=1.0),
        knowledge_units=[unverified],
        modality_warnings=["图片语义尚未解析"],
        processing_errors=[],
        duplicate_merged_count=0,
    )

    assert report.status == QualityStatus.FAIL
    assert report.anchor_coverage == 0.0
    assert any("图片" in issue for issue in report.blocking_issues)


def test_budget_deviation_over_three_points_is_reported() -> None:
    knowledge = _knowledge(
        "K1",
        "准确性是效率的前提",
        _anchor("S1", 0, "准确性是效率的前提"),
    )
    report = evaluate_quality(
        structure=StructureReport(
            valid=True, body_start=0, body_end=100, body_coverage=1.0
        ),
        span_map=SpanMapReport(
            valid=True, source_coverage=1.0, target_coverage=1.0
        ),
        unit_coverage=UnitCoverageReport(coverage=1.0),
        knowledge_units=[knowledge],
        modality_warnings=[],
        processing_errors=[],
        duplicate_merged_count=0,
        target_kept_ratio=0.15,
        actual_kept_ratio=0.22,
    )

    assert report.status == QualityStatus.FAIL
    assert report.budget_within_tolerance is False
    assert "预算" in report.budget_deviation_reason


def _write_export_fixture(tmp_path: Path, monkeypatch, status: str) -> str:
    books = tmp_path / "books"
    intermediate = tmp_path / "intermediate"
    output = tmp_path / "output"
    for path in (books, intermediate, output):
        path.mkdir()
    monkeypatch.setattr(config, "BOOKS_DIR", books)
    monkeypatch.setattr(config, "INTERMEDIATE_DIR", intermediate)
    monkeypatch.setattr(config, "OUTPUT_DIR", output)
    book_id = "book123"
    (books / f"{book_id}.meta.json").write_text(
        json.dumps(
            {
                "title": "夹具书",
                "author": "作者",
                "source_format": "md",
                "source_fingerprint": "a" * 64,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (intermediate / f"{book_id}.distilled.md").write_text(
        "# 全书知识精华\n\n- [来源主张] 准确性是效率的前提\n  - 来源 `S1` 0-10：准确性是效率的前提",
        encoding="utf-8",
    )
    (intermediate / f"{book_id}.distill.json").write_text(
        json.dumps(
            {
                "strength": "standard",
                "total_source_chars": 100,
                "total_output_chars": 15,
                "api_calls": 1,
                "anchor_coverage": 1.0,
                "unit_coverage": {"coverage": 1.0},
                "duplicate_merged_count": 1,
                "errors": [],
            }
        ),
        encoding="utf-8",
    )
    (intermediate / f"{book_id}.quality.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "status": status,
                "body_coverage": 1.0,
                "anchor_coverage": 1.0,
                "duplicate_merged_count": 1,
                "warnings": [],
                "blocking_issues": [] if status == "pass" else ["图片语义尚未解析"],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return book_id


def test_formal_export_contains_traceability_and_quality_summary(
    tmp_path: Path, monkeypatch
) -> None:
    book_id = _write_export_fixture(tmp_path, monkeypatch, "pass")

    content = build_export_md(book_id)

    assert "源文件指纹" in content
    assert "正文覆盖率: 100.0%" in content
    assert "锚点覆盖率: 100.0%" in content
    assert "来源 `S1`" in content


def test_failed_quality_allows_only_explicit_diagnostic_export(
    tmp_path: Path, monkeypatch
) -> None:
    book_id = _write_export_fixture(tmp_path, monkeypatch, "fail")

    with pytest.raises(ExportQualityError):
        build_export_md(book_id)
    diagnostic = build_export_md(book_id, diagnostic=True)

    assert "未通过质量校验的诊断稿" in diagnostic
    assert "图片语义尚未解析" in diagnostic
