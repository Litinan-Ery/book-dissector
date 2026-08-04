"""模型结构化输出、原文锚点解析与失败重试。"""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Awaitable, Callable

from pydantic import BaseModel, Field, ValidationError

from ..models.domain import (
    DistillUnit,
    KnowledgeKind,
    KnowledgeUnit,
    SourceAnchor,
    VerificationStatus,
)


class ModelCitation(BaseModel):
    source_id: str = Field(min_length=1)
    quote: str = Field(min_length=1, max_length=500)


class ModelKnowledgeItem(BaseModel):
    kind: KnowledgeKind
    content: str = Field(min_length=1)
    citations: list[ModelCitation] = Field(default_factory=list)


class ModelDistillResponse(BaseModel):
    unit_id: str = Field(min_length=1)
    items: list[ModelKnowledgeItem] = Field(min_length=1)


class EvidenceValidationError(ValueError):
    def __init__(self, issues: list[str]) -> None:
        self.issues = issues
        super().__init__("；".join(issues))


def build_structured_prompt(
    book_title: str, unit: DistillUnit, original_text: str
) -> str:
    sources: list[str] = []
    for span in unit.source_spans:
        source_text = original_text[span.start_char : span.end_char]
        sources.append(
            f"<source id=\"{span.source_id}\" start=\"{span.start_char}\" "
            f"end=\"{span.end_char}\">\n{source_text}\n</source>"
        )
    schema = {
        "unit_id": unit.unit_id,
        "items": [
            {
                "kind": "fact | source_claim | inference | hypothesis | unknown",
                "content": "精炼后的独立知识",
                "citations": [
                    {"source_id": "必须来自 source 标签", "quote": "原文中的连续短句"}
                ],
            }
        ],
    }
    return (
        f"书籍：《{book_title}》；提炼单元：{unit.title}；目标约 {unit.target_chars} 字。\n"
        "只输出一个 JSON 对象，不要输出 Markdown 或解释。每条非 unknown 知识必须引用至少一处"
        "下方原文；source_id 必须完全一致，quote 必须是对应 source 中逐字连续出现的短句。"
        "跨来源综合只能标为 inference。无法核验的内容标为 unknown，不得伪装成作者观点。\n"
        f"JSON 结构示例：{json.dumps(schema, ensure_ascii=False)}\n\n"
        + "\n\n".join(sources)
    )


def parse_model_response(raw: str) -> ModelDistillResponse:
    cleaned = raw.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", cleaned, flags=re.I | re.S)
    if fenced:
        cleaned = fenced.group(1)
    try:
        payload = json.loads(cleaned)
        return ModelDistillResponse.model_validate(payload)
    except (json.JSONDecodeError, ValidationError, TypeError) as exc:
        raise EvidenceValidationError([f"模型 JSON 格式无效：{exc}"]) from exc


def resolve_model_response(
    response: ModelDistillResponse,
    unit: DistillUnit,
    original_text: str,
) -> list[KnowledgeUnit]:
    issues: list[str] = []
    if response.unit_id != unit.unit_id:
        issues.append(
            f"unit_id 不匹配：期望 {unit.unit_id}，得到 {response.unit_id}"
        )
    allowed = {span.source_id: span for span in unit.source_spans}
    resolved: list[KnowledgeUnit] = []

    for item_index, item in enumerate(response.items):
        anchors: list[SourceAnchor] = []
        seen: set[tuple[str, str]] = set()
        if item.kind != KnowledgeKind.UNKNOWN and not item.citations:
            issues.append(f"知识项 {item_index + 1} 缺少来源引用")
        for citation in item.citations:
            key = (citation.source_id, citation.quote)
            if key in seen:
                continue
            seen.add(key)
            span = allowed.get(citation.source_id)
            if span is None:
                issues.append(
                    f"知识项 {item_index + 1} 使用了未授权来源 {citation.source_id}"
                )
                continue
            quote = citation.quote.strip()
            source_text = original_text[span.start_char : span.end_char]
            local_start = source_text.find(quote)
            if local_start < 0:
                issues.append(
                    f"知识项 {item_index + 1} 的引文不在来源 {citation.source_id} 中"
                )
                continue
            start = span.start_char + local_start
            anchors.append(
                SourceAnchor.from_text(
                    source_id=span.source_id,
                    start_char=start,
                    end_char=start + len(quote),
                    quote=quote,
                    chapter_id=span.chapter_id,
                    page=span.page,
                )
            )
        if item.kind != KnowledgeKind.UNKNOWN and not anchors:
            issues.append(f"知识项 {item_index + 1} 没有可验证锚点")
            continue

        identity_material = "|".join(
            [item.kind.value, item.content]
            + [f"{anchor.source_id}:{anchor.start_char}:{anchor.end_char}" for anchor in anchors]
        )
        resolved.append(
            KnowledgeUnit(
                knowledge_id="K"
                + hashlib.sha256(identity_material.encode("utf-8")).hexdigest()[:16].upper(),
                kind=item.kind,
                content=item.content.strip(),
                anchors=anchors,
                verification_status=(
                    VerificationStatus.VERIFIED
                    if anchors
                    else VerificationStatus.UNVERIFIED
                ),
            )
        )

    if issues:
        raise EvidenceValidationError(issues)
    return resolved


async def distill_with_validation(
    *,
    book_title: str,
    unit: DistillUnit,
    original_text: str,
    caller: Callable[[str], Awaitable[str]],
    max_attempts: int = 2,
) -> list[KnowledgeUnit]:
    if max_attempts < 1:
        raise ValueError("max_attempts must be at least 1")
    base_prompt = build_structured_prompt(book_title, unit, original_text)
    last_issues: list[str] = []
    for attempt in range(max_attempts):
        prompt = base_prompt
        if attempt:
            prompt = (
                "上一次输出未通过程序校验："
                + "；".join(last_issues)
                + "。请修正后重新输出完整 JSON。\n\n"
                + base_prompt
            )
        try:
            raw = await caller(prompt)
            parsed = parse_model_response(raw)
            return resolve_model_response(parsed, unit, original_text)
        except EvidenceValidationError as exc:
            last_issues = exc.issues
    raise EvidenceValidationError(last_issues or ["模型输出未通过证据校验"])
