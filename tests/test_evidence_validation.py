import asyncio
import json

import pytest

from app.core.evidence import (
    EvidenceValidationError,
    ModelCitation,
    ModelDistillResponse,
    ModelKnowledgeItem,
    build_structured_prompt,
    distill_with_validation,
    parse_model_response,
    resolve_model_response,
)
from app.models.domain import (
    DistillUnit,
    KnowledgeKind,
    SourceSpan,
    VerificationStatus,
)


ORIGINAL = "作者明确主张效率必须建立在准确性之上。随后给出实验数据。"
SPAN = SourceSpan(source_id="SABC123", start_char=0, end_char=len(ORIGINAL))
UNIT = DistillUnit(
    unit_id="UABC123",
    title="第一章",
    target_start=0,
    target_end=len(ORIGINAL),
    input_text=ORIGINAL,
    source_spans=[SPAN],
    input_fingerprint="a" * 64,
    target_chars=20,
)


def _valid_response() -> ModelDistillResponse:
    return ModelDistillResponse(
        unit_id=UNIT.unit_id,
        items=[
            ModelKnowledgeItem(
                kind=KnowledgeKind.SOURCE_CLAIM,
                content="准确性是效率的前提。",
                citations=[
                    ModelCitation(
                        source_id=SPAN.source_id,
                        quote="效率必须建立在准确性之上",
                    )
                ],
            )
        ],
    )


def test_valid_model_claim_resolves_to_exact_verified_anchor() -> None:
    units = resolve_model_response(_valid_response(), UNIT, ORIGINAL)

    assert len(units) == 1
    assert units[0].verification_status == VerificationStatus.VERIFIED
    assert units[0].anchors[0].quote == "效率必须建立在准确性之上"
    assert ORIGINAL[
        units[0].anchors[0].start_char : units[0].anchors[0].end_char
    ] == units[0].anchors[0].quote


@pytest.mark.parametrize(
    ("source_id", "quote"),
    [
        ("SNOTALLOWED", "效率必须建立在准确性之上"),
        (SPAN.source_id, "原文从未出现的虚构数据"),
    ],
)
def test_invalid_source_or_invented_quote_is_rejected(source_id: str, quote: str) -> None:
    response = ModelDistillResponse(
        unit_id=UNIT.unit_id,
        items=[
            ModelKnowledgeItem(
                kind=KnowledgeKind.FACT,
                content="不可信内容",
                citations=[ModelCitation(source_id=source_id, quote=quote)],
            )
        ],
    )

    with pytest.raises(EvidenceValidationError):
        resolve_model_response(response, UNIT, ORIGINAL)


def test_non_unknown_knowledge_without_citation_is_rejected() -> None:
    response = ModelDistillResponse(
        unit_id=UNIT.unit_id,
        items=[
            ModelKnowledgeItem(
                kind=KnowledgeKind.SOURCE_CLAIM,
                content="没有证据",
                citations=[],
            )
        ],
    )

    with pytest.raises(EvidenceValidationError):
        resolve_model_response(response, UNIT, ORIGINAL)


def test_unknown_item_is_retained_only_as_unverified_issue() -> None:
    response = ModelDistillResponse(
        unit_id=UNIT.unit_id,
        items=[
            ModelKnowledgeItem(
                kind=KnowledgeKind.UNKNOWN,
                content="无法核验",
                citations=[],
            )
        ],
    )

    units = resolve_model_response(response, UNIT, ORIGINAL)

    assert units[0].verification_status == VerificationStatus.UNVERIFIED
    assert units[0].anchors == []


def test_prompt_exposes_only_allowed_source_ids_and_exact_text() -> None:
    prompt = build_structured_prompt("夹具书", UNIT, ORIGINAL)

    assert SPAN.source_id in prompt
    assert ORIGINAL in prompt
    assert '"kind"' in prompt
    assert '"citations"' in prompt


def test_parser_accepts_json_code_fence_but_validates_schema() -> None:
    raw = "```json\n" + json.dumps(_valid_response().model_dump(mode="json"), ensure_ascii=False) + "\n```"

    parsed = parse_model_response(raw)

    assert parsed.unit_id == UNIT.unit_id


def test_invalid_first_response_is_retried_then_accepted() -> None:
    bad = _valid_response().model_copy(deep=True)
    bad.items[0].citations[0].quote = "虚构引文"
    responses = [bad, _valid_response()]
    calls: list[str] = []

    async def caller(prompt: str) -> str:
        calls.append(prompt)
        return json.dumps(responses.pop(0).model_dump(mode="json"), ensure_ascii=False)

    result = asyncio.run(
        distill_with_validation(
            book_title="夹具书",
            unit=UNIT,
            original_text=ORIGINAL,
            caller=caller,
            max_attempts=2,
        )
    )

    assert len(calls) == 2
    assert "上一次输出未通过" in calls[1]
    assert result[0].verification_status == VerificationStatus.VERIFIED

