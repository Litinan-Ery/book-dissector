"""P0/P1/P2 共享的版本化领域数据契约。

这些模型是原文、删减、提炼与质量报告之间的稳定边界。模型输出不能
绕过它们直接成为正式精华稿。
"""
from __future__ import annotations

import hashlib
from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field, model_validator

SCHEMA_VERSION = "1.0"


class VersionedModel(BaseModel):
    schema_version: str = SCHEMA_VERSION


class Modality(str, Enum):
    TEXT = "text"
    HEADING = "heading"
    TABLE = "table"
    IMAGE = "image"
    FORMULA = "formula"
    CODE = "code"
    FOOTNOTE = "footnote"


class SpanMapStatus(str, Enum):
    KEPT = "kept"
    DELETED = "deleted"


class KnowledgeKind(str, Enum):
    FACT = "fact"
    SOURCE_CLAIM = "source_claim"
    INFERENCE = "inference"
    HYPOTHESIS = "hypothesis"
    UNKNOWN = "unknown"


class VerificationStatus(str, Enum):
    VERIFIED = "verified"
    INVALID = "invalid"
    UNVERIFIED = "unverified"


class QualityStatus(str, Enum):
    PASS = "pass"
    WARNING = "warning"
    FAIL = "fail"


class CharacterRange(VersionedModel):
    start: int = Field(ge=0)
    end: int = Field(gt=0)

    @model_validator(mode="after")
    def validate_range(self) -> CharacterRange:
        if self.end <= self.start:
            raise ValueError("character range is empty")
        return self


class StructureIssue(VersionedModel):
    code: str
    message: str
    blocking: bool = True
    chapter_title: str = ""
    start: int | None = None
    end: int | None = None


class StructureReport(VersionedModel):
    valid: bool
    body_start: int = Field(ge=0)
    body_end: int = Field(ge=0)
    body_coverage: float = Field(ge=0, le=1)
    uncovered_ranges: list[CharacterRange] = Field(default_factory=list)
    duplicate_ranges: list[CharacterRange] = Field(default_factory=list)
    issues: list[StructureIssue] = Field(default_factory=list)


class SpanMapReport(VersionedModel):
    valid: bool
    source_coverage: float = Field(ge=0, le=1)
    target_coverage: float = Field(ge=0, le=1)
    issues: list[str] = Field(default_factory=list)


class RunStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"
    QUALITY_FAILED = "quality_failed"


class SourceSpan(VersionedModel):
    """原文中的稳定字符区间。"""

    source_id: str = Field(min_length=1, max_length=80)
    start_char: int = Field(ge=0)
    end_char: int = Field(gt=0)
    chapter_id: str | None = None
    page: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def validate_range(self) -> SourceSpan:
        if self.end_char <= self.start_char:
            raise ValueError("source span end_char must be greater than start_char")
        return self

    @property
    def length(self) -> int:
        return self.end_char - self.start_char


class SourceAnchor(SourceSpan):
    """带核验片段与指纹的原文锚点。"""

    quote: str = Field(min_length=1, max_length=500)
    quote_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")

    @classmethod
    def from_text(
        cls,
        *,
        source_id: str,
        start_char: int,
        end_char: int,
        quote: str,
        chapter_id: str | None = None,
        page: int | None = None,
    ) -> SourceAnchor:
        fingerprint = hashlib.sha256(quote.encode("utf-8")).hexdigest()
        return cls(
            source_id=source_id,
            start_char=start_char,
            end_char=end_char,
            chapter_id=chapter_id,
            page=page,
            quote=quote,
            quote_fingerprint=fingerprint,
        )


class ContentBlock(VersionedModel):
    block_id: str = Field(min_length=1, max_length=80)
    modality: Modality
    source_span: SourceSpan
    text: str = ""
    parse_warning: str = ""


class SpanMapEntry(VersionedModel):
    """原文区间到删减稿区间的一段可逆映射。"""

    source_start: int = Field(ge=0)
    source_end: int = Field(gt=0)
    target_start: int | None = Field(default=None, ge=0)
    target_end: int | None = Field(default=None, ge=0)
    status: SpanMapStatus
    reason: str = ""

    @model_validator(mode="after")
    def validate_mapping(self) -> SpanMapEntry:
        if self.source_end <= self.source_start:
            raise ValueError("source mapping range is empty")
        if self.status == SpanMapStatus.KEPT:
            if self.target_start is None or self.target_end is None:
                raise ValueError("kept mapping requires a target range")
            if self.target_end <= self.target_start:
                raise ValueError("target mapping range is empty")
            if self.source_length != self.target_length:
                raise ValueError("kept mapping must preserve character length")
        else:
            if self.target_start is not None or self.target_end is not None:
                raise ValueError("deleted mapping cannot have a target range")
            if not self.reason.strip():
                raise ValueError("deleted mapping requires a reason")
        return self

    @property
    def source_length(self) -> int:
        return self.source_end - self.source_start

    @property
    def target_length(self) -> int:
        if self.target_start is None or self.target_end is None:
            return 0
        return self.target_end - self.target_start


class DistillUnit(VersionedModel):
    unit_id: str = Field(min_length=1, max_length=80)
    title: str
    source_spans: list[SourceSpan] = Field(min_length=1)
    input_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    target_chars: int = Field(ge=1)
    status: str = "pending"


class KnowledgeUnit(VersionedModel):
    knowledge_id: str = Field(min_length=1, max_length=80)
    kind: KnowledgeKind
    content: str = Field(min_length=1)
    anchors: list[SourceAnchor] = Field(default_factory=list)
    verification_status: VerificationStatus = VerificationStatus.UNVERIFIED
    duplicate_of: str | None = None

    @model_validator(mode="after")
    def verified_knowledge_requires_anchor(self) -> KnowledgeUnit:
        if self.kind != KnowledgeKind.UNKNOWN and not self.anchors:
            raise ValueError("knowledge unit requires at least one source anchor")
        if self.verification_status == VerificationStatus.VERIFIED and not self.anchors:
            raise ValueError("verified knowledge unit requires an anchor")
        return self


class BudgetFactors(VersionedModel):
    relevance: float = Field(ge=0, le=1)
    novelty: float = Field(ge=0, le=1)
    dependency: float = Field(ge=0, le=1)
    evidence_value: float = Field(ge=0, le=1)


class BudgetAllocation(VersionedModel):
    unit_id: str
    factors: BudgetFactors
    target_chars: int = Field(ge=1)
    reason: str


class BudgetPlan(VersionedModel):
    book_id: str
    book_type: str
    strength: str
    total_source_chars: int = Field(ge=0)
    total_target_chars: int = Field(ge=0)
    allocations: list[BudgetAllocation] = Field(default_factory=list)


class QualityReport(VersionedModel):
    status: QualityStatus
    body_coverage: float = Field(ge=0, le=1)
    anchor_coverage: float = Field(ge=0, le=1)
    duplicate_merged_count: int = Field(default=0, ge=0)
    warnings: list[str] = Field(default_factory=list)
    blocking_issues: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def pass_has_no_blockers(self) -> QualityReport:
        if self.status == QualityStatus.PASS and self.blocking_issues:
            raise ValueError("quality report cannot pass with blocking issues")
        return self


class RunManifest(VersionedModel):
    run_id: str = Field(pattern=r"^[A-Za-z0-9_-]+$")
    book_id: str = Field(min_length=1, max_length=80)
    source_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: datetime
    status: RunStatus = RunStatus.PENDING
    current_stage: str = "created"
    book_type: str = "general"
    strength: str = "standard"
    model: str = "deepseek-v4-flash"
    prompt_version: str = "1.0"
