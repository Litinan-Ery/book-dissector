"""全书知识单元去重，合并观点但保留全部独立证据。"""
from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher

from ..models.domain import KnowledgeKind, KnowledgeUnit


@dataclass
class DedupResult:
    units: list[KnowledgeUnit]
    merged_count: int


_PREFIX_RE = re.compile(
    r"^(?:作者(?:指出|认为|强调|主张)[：,:，]?|本章(?:指出|认为|强调|主张)[：,:，]?|"
    r"换言之[：,:，]?|也就是说[：,:，]?)"
)
_PUNCTUATION_RE = re.compile(r"[\s\W_]+", re.UNICODE)
_OPPOSITES = [
    ("提高", "降低"),
    ("增加", "减少"),
    ("支持", "反对"),
    ("扩大", "缩小"),
    ("允许", "禁止"),
    ("可以", "不可以"),
    ("是", "不是"),
]


def _canonical(content: str) -> str:
    cleaned = content.strip().lower()
    while True:
        stripped = _PREFIX_RE.sub("", cleaned, count=1)
        if stripped == cleaned:
            break
        cleaned = stripped
    return _PUNCTUATION_RE.sub("", cleaned)


def _contradictory(left: str, right: str) -> bool:
    for positive, negative in _OPPOSITES:
        if (positive in left and negative in right) or (
            negative in left and positive in right
        ):
            return True
    return False


def _same_knowledge(left: KnowledgeUnit, right: KnowledgeUnit) -> bool:
    if left.kind != right.kind:
        return False
    left_text = _canonical(left.content)
    right_text = _canonical(right.content)
    if not left_text or not right_text or _contradictory(left_text, right_text):
        return False
    if left_text == right_text:
        return True
    left_sources = {
        (anchor.source_id, anchor.start_char, anchor.end_char) for anchor in left.anchors
    }
    right_sources = {
        (anchor.source_id, anchor.start_char, anchor.end_char) for anchor in right.anchors
    }
    if left_sources & right_sources:
        return True
    shorter, longer = sorted((left_text, right_text), key=len)
    if shorter in longer and len(shorter) / len(longer) >= 0.72:
        return True
    return SequenceMatcher(None, left_text, right_text).ratio() >= 0.9


def _merge_into(target: KnowledgeUnit, duplicate: KnowledgeUnit) -> None:
    known = {
        (anchor.source_id, anchor.start_char, anchor.end_char) for anchor in target.anchors
    }
    for anchor in duplicate.anchors:
        key = (anchor.source_id, anchor.start_char, anchor.end_char)
        if key not in known:
            target.anchors.append(anchor)
            known.add(key)
    for unit_id in duplicate.origin_unit_ids:
        if unit_id not in target.origin_unit_ids:
            target.origin_unit_ids.append(unit_id)


def merge_knowledge_units(units: list[KnowledgeUnit]) -> DedupResult:
    merged: list[KnowledgeUnit] = []
    merged_count = 0
    for unit in units:
        candidate = next((item for item in merged if _same_knowledge(item, unit)), None)
        if candidate is None:
            merged.append(unit.model_copy(deep=True))
            continue
        _merge_into(candidate, unit)
        merged_count += 1
    return DedupResult(units=merged, merged_count=merged_count)


def find_duplicate_pairs(units: list[KnowledgeUnit]) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for index, left in enumerate(units):
        for right in units[index + 1 :]:
            if _same_knowledge(left, right):
                pairs.append((left.knowledge_id, right.knowledge_id))
    return pairs

