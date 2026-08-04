"""全书定向扫描与确定性的自适应预算分配。"""
from __future__ import annotations

import math
import re
from collections import Counter
from difflib import SequenceMatcher

from ..models.domain import (
    BudgetAllocation,
    BudgetFactors,
    BudgetPlan,
    DistillUnit,
    OrientationScan,
    OrientationUnitScore,
)

STRENGTH_RATIOS = {
    "conservative": 0.25,
    "standard": 0.15,
    "aggressive": 0.08,
}
TYPE_FACTORS = {"general": 1.0, "fiction": 0.7, "technical": 1.3}

_MECHANISM = ("核心", "机制", "方法", "步骤", "流程", "模型", "如何", "规则")
_PREREQUISITE = ("定义", "概念", "前提", "基础", "首先", "必要", "依赖")
_EVIDENCE = ("数据", "实验", "案例", "证据", "调查", "公式", "参数", "代码", "%")
_BACKGROUND = ("背景", "历史", "概述", "沿革", "简介")
_DUPLICATE = ("重复", "回顾", "复述", "再次", "小结")
_FICTION = ("人物", "冲突", "转折", "高潮", "结局", "伏笔", "场景")


def _clip(value: float) -> float:
    return round(min(1.0, max(0.0, value)), 6)


def _normalized(text: str) -> str:
    return re.sub(r"[\s\W_]+", "", text.lower(), flags=re.UNICODE)


def _contains_any(text: str, words: tuple[str, ...]) -> bool:
    return any(word in text for word in words)


def _core_terms(units: list[DistillUnit]) -> list[str]:
    counter: Counter[str] = Counter()
    for unit in units:
        normalized = _normalized(unit.input_text)
        counter.update(
            normalized[index : index + 2]
            for index in range(max(0, len(normalized) - 1))
            if normalized[index : index + 2]
        )
    return [term for term, count in counter.most_common(12) if count >= 2]


def _role_and_factors(
    unit: DistillUnit,
    previous: list[DistillUnit],
    book_type: str,
    core_terms: list[str],
) -> tuple[str, BudgetFactors]:
    text = unit.title + "\n" + unit.input_text
    normalized = _normalized(unit.input_text)
    max_similarity = max(
        (SequenceMatcher(None, normalized, _normalized(item.input_text)).ratio() for item in previous),
        default=0.0,
    )
    duplicate = _contains_any(unit.title, _DUPLICATE) or max_similarity >= 0.9
    mechanism = _contains_any(text, _MECHANISM)
    prerequisite = _contains_any(text, _PREREQUISITE)
    background = _contains_any(text, _BACKGROUND)
    evidence = _contains_any(text, _EVIDENCE) or bool(re.search(r"\d", text))
    fiction = _contains_any(text, _FICTION)

    if duplicate:
        role = "duplicate"
    elif mechanism:
        role = "mechanism"
    elif prerequisite:
        role = "prerequisite"
    elif fiction and book_type == "fiction":
        role = "key_scene"
    elif background:
        role = "background"
    else:
        role = "supporting"

    term_hits = sum(term in normalized for term in core_terms)
    relevance = 0.45 + min(0.15, term_hits * 0.02)
    if mechanism:
        relevance += 0.3
    if prerequisite:
        relevance += 0.18
    if background:
        relevance -= 0.18
    if duplicate:
        relevance -= 0.2
    if fiction and book_type == "fiction":
        relevance += 0.25

    novelty = max(0.05, 1.0 - max_similarity)
    if not previous:
        novelty = 0.85
    if duplicate:
        novelty = min(novelty, 0.08)

    dependency = 0.3
    if prerequisite:
        dependency = 0.95
    elif mechanism:
        dependency = 0.72
    elif background:
        dependency = 0.18
    if duplicate:
        dependency = min(dependency, 0.25)

    evidence_value = 0.25
    if evidence:
        evidence_value += 0.45
    if mechanism:
        evidence_value += 0.18
    if background:
        evidence_value -= 0.1
    if duplicate:
        evidence_value -= 0.15
    if fiction and book_type == "fiction":
        evidence_value += 0.25
    if book_type == "technical":
        dependency += 0.08
        evidence_value += 0.08

    return role, BudgetFactors(
        relevance=_clip(relevance),
        novelty=_clip(novelty),
        dependency=_clip(dependency),
        evidence_value=_clip(evidence_value),
    )


def _weights_for(book_type: str) -> tuple[float, float, float, float]:
    if book_type == "technical":
        return 0.25, 0.2, 0.3, 0.25
    if book_type == "fiction":
        return 0.3, 0.35, 0.15, 0.2
    return 0.35, 0.3, 0.2, 0.15


def scan_orientation(
    units: list[DistillUnit], *, book_type: str
) -> OrientationScan:
    if book_type not in TYPE_FACTORS:
        raise ValueError(f"未知书籍类型：{book_type}")
    terms = _core_terms(units)
    weights = _weights_for(book_type)
    scores: list[OrientationUnitScore] = []
    previous: list[DistillUnit] = []
    for unit in units:
        role, factors = _role_and_factors(unit, previous, book_type, terms)
        combined = (
            factors.relevance * weights[0]
            + factors.novelty * weights[1]
            + factors.dependency * weights[2]
            + factors.evidence_value * weights[3]
        )
        reason = (
            f"角色={role}；相关性={factors.relevance:.2f}；新颖度={factors.novelty:.2f}；"
            f"前置依赖={factors.dependency:.2f}；证据价值={factors.evidence_value:.2f}"
        )
        scores.append(
            OrientationUnitScore(
                unit_id=unit.unit_id,
                role=role,
                factors=factors,
                combined_score=round(max(0.01, combined), 6),
                reason=reason,
            )
        )
        previous.append(unit)
    return OrientationScan(book_type=book_type, core_terms=terms, scores=scores)


def _union_length(units: list[DistillUnit]) -> int:
    intervals = sorted((unit.target_start, unit.target_end) for unit in units)
    if not intervals:
        return 0
    total = 0
    start, end = intervals[0]
    for next_start, next_end in intervals[1:]:
        if next_start > end:
            total += end - start
            start, end = next_start, next_end
        else:
            end = max(end, next_end)
    return total + end - start


def allocate_budget(
    units: list[DistillUnit],
    scan: OrientationScan,
    *,
    book_id: str,
    book_type: str,
    strength: str,
) -> BudgetPlan:
    if not units:
        raise ValueError("没有可分配预算的提炼单元")
    if strength not in STRENGTH_RATIOS:
        raise ValueError(f"未知压缩强度：{strength}")
    if scan.book_type != book_type:
        raise ValueError("定向扫描的书籍类型与预算请求不一致")
    by_id = {score.unit_id: score for score in scan.scores}
    if set(by_id) != {unit.unit_id for unit in units}:
        raise ValueError("定向扫描未覆盖全部提炼单元")

    total_source = _union_length(units)
    ratio = STRENGTH_RATIOS[strength] * TYPE_FACTORS[book_type]
    total_target = max(len(units), round(total_source * ratio))
    weighted = [
        max(0.01, by_id[unit.unit_id].combined_score) * len(unit.input_text)
        for unit in units
    ]
    weight_sum = sum(weighted)
    exact = [total_target * value / weight_sum for value in weighted]
    targets = [max(1, math.floor(value)) for value in exact]
    difference = total_target - sum(targets)
    order = sorted(
        range(len(units)),
        key=lambda index: (exact[index] - math.floor(exact[index]), -index),
        reverse=True,
    )
    cursor = 0
    while difference > 0:
        targets[order[cursor % len(order)]] += 1
        difference -= 1
        cursor += 1
    while difference < 0:
        candidates = [index for index in reversed(order) if targets[index] > 1]
        if not candidates:
            break
        targets[candidates[cursor % len(candidates)]] -= 1
        difference += 1
        cursor += 1

    allocations = [
        BudgetAllocation(
            unit_id=unit.unit_id,
            factors=by_id[unit.unit_id].factors,
            target_chars=target,
            reason=by_id[unit.unit_id].reason,
        )
        for unit, target in zip(units, targets)
    ]
    return BudgetPlan(
        book_id=book_id,
        book_type=book_type,
        strength=strength,
        total_source_chars=total_source,
        total_target_chars=sum(targets),
        allocations=allocations,
    )


def apply_budget_plan(
    units: list[DistillUnit], plan: BudgetPlan
) -> list[DistillUnit]:
    targets = {allocation.unit_id: allocation.target_chars for allocation in plan.allocations}
    if set(targets) != {unit.unit_id for unit in units}:
        raise ValueError("预算清单未覆盖全部提炼单元")
    return [
        unit.model_copy(update={"target_chars": targets[unit.unit_id]}, deep=True)
        for unit in units
    ]

