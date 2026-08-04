"""带稳定来源 ID、结构化知识单元与锚点校验的提炼器。"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from typing import Callable

import httpx

from .. import config
from ..models.domain import (
    DistillUnit,
    KnowledgeKind,
    KnowledgeUnit,
    SpanMapEntry,
    UnitCoverageReport,
    VerificationStatus,
)
from .evidence import EvidenceValidationError, distill_with_validation
from .extractors.base import Chapter
from .quality import validate_structure
from .spanmap import build_span_map, validate_span_map
from .units import build_distill_units, validate_unit_coverage

MAX_CHUNK_CHARS = 12000
OVERLAP_CHARS = 500

STRENGTH_RATIOS = {
    "conservative": 0.25,
    "standard": 0.15,
    "aggressive": 0.08,
}
DEFAULT_STRENGTH = "standard"
TYPE_FACTORS = {"general": 1.0, "fiction": 0.7, "technical": 1.3}

ProgressCb = Callable[[str, int, int], None]


class QualityGateError(RuntimeError):
    """模型调用之前的硬质量门禁失败。"""


@dataclass
class ChapterDistill:
    title: str
    source_chars: int
    target_chars: int
    output_chars: int
    text: str = ""
    error: str = ""
    unit_id: str = ""


@dataclass
class DistillResult:
    book_title: str
    book_type: str
    strength: str
    chapters: list[ChapterDistill] = field(default_factory=list)
    distill_units: list[DistillUnit] = field(default_factory=list)
    knowledge_units: list[KnowledgeUnit] = field(default_factory=list)
    unit_coverage: UnitCoverageReport = field(
        default_factory=lambda: UnitCoverageReport(coverage=0.0)
    )
    total_source_chars: int = 0
    total_output_chars: int = 0
    api_calls: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def anchor_coverage(self) -> float:
        core = [
            unit for unit in self.knowledge_units if unit.kind != KnowledgeKind.UNKNOWN
        ]
        if not core:
            return 0.0
        anchored = sum(
            unit.verification_status == VerificationStatus.VERIFIED and bool(unit.anchors)
            for unit in core
        )
        return round(anchored / len(core), 6)

    @property
    def final_text(self) -> str:
        return "\n\n".join(
            chapter.text.strip() for chapter in self.chapters if chapter.text.strip()
        )


def _ratio_for(book_type: str, strength: str) -> float:
    base = STRENGTH_RATIOS.get(strength, STRENGTH_RATIOS[DEFAULT_STRENGTH])
    return base * TYPE_FACTORS.get(book_type, 1.0)


def _system_prompt(book_type: str) -> str:
    focus = {
        "general": "保留作者核心观点、论据、边界与关键例子",
        "fiction": "保留人物、主线事件、转折、关键场景与结局",
        "technical": "保留定义、前置知识、步骤、参数、公式、代码要点与限制",
    }[book_type]
    return (
        "你是忠实的图书知识提炼器。"
        + focus
        + "。不得补写原文没有的事实；必须按用户消息给定的 JSON 结构输出，并逐条引用原文。"
    )


async def _call_deepseek(
    client: httpx.AsyncClient,
    system: str,
    user: str,
    api_key: str,
) -> str:
    payload = {
        "model": config.DEEPSEEK_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0.1,
        "max_tokens": 4096,
        "response_format": {"type": "json_object"},
    }
    response = await client.post(
        f"{config.DEEPSEEK_BASE_URL}/chat/completions",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json=payload,
    )
    if response.status_code == 401:
        raise RuntimeError("API Key 无效（401），请在设置中重新填写")
    if response.status_code == 429:
        raise RuntimeError("请求过于频繁（429），请稍后重试")
    if response.status_code != 200:
        raise RuntimeError(f"DeepSeek API 错误（HTTP {response.status_code}）")
    try:
        return response.json()["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError("DeepSeek 响应格式异常（缺少 choices/message/content）") from exc


def _fake_response(unit: DistillUnit, original_text: str) -> str:
    span = unit.source_spans[0]
    source = original_text[span.start_char : span.end_char]
    quote = source.strip()[: min(60, len(source.strip()))]
    if not quote:
        quote = source[:1]
    payload = {
        "unit_id": unit.unit_id,
        "items": [
            {
                "kind": "source_claim",
                "content": f"测试提炼：{quote}",
                "citations": [{"source_id": span.source_id, "quote": quote}],
            }
        ],
    }
    return json.dumps(payload, ensure_ascii=False)


def _render_knowledge(title: str, items: list[KnowledgeUnit]) -> str:
    labels = {
        KnowledgeKind.FACT: "事实",
        KnowledgeKind.SOURCE_CLAIM: "来源主张",
        KnowledgeKind.INFERENCE: "推断",
        KnowledgeKind.HYPOTHESIS: "假设",
        KnowledgeKind.UNKNOWN: "未知",
    }
    verified = [
        item
        for item in items
        if item.verification_status == VerificationStatus.VERIFIED and item.anchors
    ]
    if not verified:
        return ""
    lines = [f"## {title}"]
    for item in verified:
        lines.append(f"- [{labels[item.kind]}] {item.content}")
        for anchor in item.anchors:
            quote = anchor.quote.replace("\n", " ")
            lines.append(
                f"  - 来源 `{anchor.source_id}` {anchor.start_char}-{anchor.end_char}：{quote}"
            )
    return "\n".join(lines)


def _load_json(path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


async def distill_book(
    book_id: str,
    book_type: str = "general",
    strength: str = DEFAULT_STRENGTH,
    progress: ProgressCb | None = None,
    use_fake: bool | None = None,
) -> DistillResult:
    if book_type not in TYPE_FACTORS:
        raise ValueError(f"未知书籍类型：{book_type}")
    if strength not in STRENGTH_RATIOS:
        raise ValueError(f"未知压缩强度：{strength}")
    if use_fake is None:
        use_fake = os.environ.get("BOOK_DISSECTOR_FAKE_DEEPSEEK") == "1"
    api_key = config.get_api_key()
    if not use_fake and not api_key:
        raise RuntimeError("尚未配置 DeepSeek API Key，请先在设置中填写")

    original_path = config.BOOKS_DIR / f"{book_id}.txt"
    if not original_path.exists():
        raise FileNotFoundError("未找到书籍文本，请先上传并完成提取")
    original_text = original_path.read_text(encoding="utf-8")
    meta = _load_json(config.BOOKS_DIR / f"{book_id}.meta.json")
    book_title = meta.get("title") or book_id
    original_chapters = [
        Chapter(
            item.get("title", ""),
            item.get("level", 1),
            item.get("start_char", 0),
            item.get("end_char", len(original_text)),
        )
        for item in meta.get("chapters", [])
    ]
    structure_data = meta.get("structure_report")
    if structure_data is not None and not structure_data.get("valid", False):
        raise QualityGateError("原文结构质量门禁失败，禁止调用模型")
    if structure_data is None and not validate_structure(
        original_text, original_chapters
    ).valid:
        raise QualityGateError("原文结构质量门禁失败，禁止调用模型")

    pruned_path = config.INTERMEDIATE_DIR / f"{book_id}.pruned.txt"
    prune_meta = _load_json(config.INTERMEDIATE_DIR / f"{book_id}.prune.json")
    text = pruned_path.read_text(encoding="utf-8") if pruned_path.exists() else original_text
    if prune_meta.get("span_map"):
        span_map = [SpanMapEntry.model_validate(item) for item in prune_meta["span_map"]]
    else:
        span_map = build_span_map(len(original_text), [])
    mapping_report = validate_span_map(original_text, text, span_map)
    if not mapping_report.valid:
        raise QualityGateError("删减映射质量门禁失败，禁止调用模型")

    chapters_data = prune_meta.get("pruned_chapters") or meta.get("chapters", [])
    chapters = [
        Chapter(
            item.get("title", ""),
            item.get("level", 1),
            item.get("start_char", 0),
            item.get("end_char", len(text)),
        )
        for item in chapters_data
    ]
    pruned_structure = validate_structure(text, chapters)
    if not pruned_structure.valid:
        raise QualityGateError("删减稿结构质量门禁失败，禁止调用模型")

    source_fingerprint = meta.get("source_fingerprint") or hashlib.sha256(
        original_text.encode("utf-8")
    ).hexdigest()
    units = build_distill_units(
        text=text,
        chapters=chapters,
        span_map=span_map,
        source_fingerprint=source_fingerprint,
        max_chars=MAX_CHUNK_CHARS,
        overlap_chars=min(OVERLAP_CHARS, MAX_CHUNK_CHARS - 1),
        target_ratio=_ratio_for(book_type, strength),
    )
    coverage = validate_unit_coverage(
        units,
        body_start=pruned_structure.body_start,
        body_end=pruned_structure.body_end,
    )
    if coverage.coverage < 0.99:
        raise QualityGateError(
            f"提炼单元正文覆盖率 {coverage.coverage:.2%} 低于 99%，禁止调用模型"
        )

    result = DistillResult(
        book_title=book_title,
        book_type=book_type,
        strength=strength,
        distill_units=units,
        unit_coverage=coverage,
        total_source_chars=pruned_structure.body_end - pruned_structure.body_start,
    )
    system = _system_prompt(book_type)
    async with httpx.AsyncClient(timeout=180) as client:
        for index, unit in enumerate(units):
            if progress:
                progress(f"提炼并核验：{unit.title}", index, len(units))

            async def caller(prompt: str, current_unit: DistillUnit = unit) -> str:
                result.api_calls += 1
                if use_fake:
                    return _fake_response(current_unit, original_text)
                return await _call_deepseek(client, system, prompt, api_key)

            error = ""
            knowledge: list[KnowledgeUnit] = []
            try:
                knowledge = await distill_with_validation(
                    book_title=book_title,
                    unit=unit,
                    original_text=original_text,
                    caller=caller,
                    max_attempts=2,
                )
                result.knowledge_units.extend(knowledge)
            except (EvidenceValidationError, httpx.HTTPError, RuntimeError) as exc:
                error = str(exc)
                result.errors.append(f"{unit.title}：{exc}")
            rendered = _render_knowledge(unit.title, knowledge)
            result.chapters.append(
                ChapterDistill(
                    title=unit.title,
                    source_chars=len(unit.input_text),
                    target_chars=unit.target_chars,
                    output_chars=len(rendered),
                    text=rendered,
                    error=error,
                    unit_id=unit.unit_id,
                )
            )

    result.total_output_chars = len(result.final_text)
    if progress:
        progress("完成", len(units), len(units))
    return result

