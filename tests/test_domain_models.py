from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.models.domain import (
    ContentBlock,
    KnowledgeKind,
    KnowledgeUnit,
    Modality,
    QualityReport,
    QualityStatus,
    RunManifest,
    SourceAnchor,
    SourceSpan,
    SpanMapEntry,
    SpanMapStatus,
    VerificationStatus,
)


def test_source_span_requires_a_non_empty_legal_range() -> None:
    with pytest.raises(ValidationError):
        SourceSpan(source_id="S000001", start_char=12, end_char=12)
    with pytest.raises(ValidationError):
        SourceSpan(source_id="S000001", start_char=-1, end_char=2)


def test_span_map_keeps_explicit_source_to_target_relationship() -> None:
    entry = SpanMapEntry(
        source_start=10,
        source_end=20,
        target_start=8,
        target_end=18,
        status=SpanMapStatus.KEPT,
    )
    assert entry.source_length == 10
    assert entry.target_length == 10

    with pytest.raises(ValidationError):
        SpanMapEntry(
            source_start=10,
            source_end=20,
            target_start=8,
            target_end=17,
            status=SpanMapStatus.KEPT,
        )


def test_deleted_span_map_has_no_target_range_and_records_reason() -> None:
    entry = SpanMapEntry(
        source_start=20,
        source_end=30,
        status=SpanMapStatus.DELETED,
        reason="copyright",
    )
    assert entry.target_start is None
    assert entry.target_end is None

    with pytest.raises(ValidationError):
        SpanMapEntry(
            source_start=20,
            source_end=30,
            status=SpanMapStatus.DELETED,
        )


def test_knowledge_unit_requires_anchor_unless_marked_unknown() -> None:
    with pytest.raises(ValidationError):
        KnowledgeUnit(
            knowledge_id="K000001",
            kind=KnowledgeKind.SOURCE_CLAIM,
            content="作者主张。",
            verification_status=VerificationStatus.VERIFIED,
        )

    unknown = KnowledgeUnit(
        knowledge_id="K000002",
        kind=KnowledgeKind.UNKNOWN,
        content="无法核验的内容。",
        verification_status=VerificationStatus.UNVERIFIED,
    )
    assert unknown.anchors == []


def test_content_block_and_manifest_are_versioned() -> None:
    block = ContentBlock(
        block_id="B000001",
        modality=Modality.TEXT,
        source_span=SourceSpan(source_id="S000001", start_char=0, end_char=5),
        text="正文内容",
    )
    manifest = RunManifest(
        run_id="run_20260805_abcdef12",
        book_id="book123",
        source_fingerprint="a" * 64,
        created_at=datetime(2026, 8, 5, tzinfo=UTC),
    )
    assert block.schema_version == "1.0"
    assert manifest.schema_version == "1.0"


def test_quality_report_cannot_pass_with_blocking_issues() -> None:
    with pytest.raises(ValidationError):
        QualityReport(
            status=QualityStatus.PASS,
            body_coverage=0.99,
            anchor_coverage=1.0,
            blocking_issues=["章节重叠"],
        )


def test_source_anchor_contains_verifiable_quote_and_fingerprint() -> None:
    anchor = SourceAnchor.from_text(
        source_id="S000001",
        start_char=4,
        end_char=8,
        quote="关键证据",
    )
    assert anchor.quote == "关键证据"
    assert len(anchor.quote_fingerprint) == 64

