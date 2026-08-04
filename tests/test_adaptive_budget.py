import hashlib

from app.core.budget import (
    allocate_budget,
    apply_budget_plan,
    scan_orientation,
)
from app.models.domain import DistillUnit, SourceSpan


def _unit(unit_id: str, title: str, text: str, start: int) -> DistillUnit:
    return DistillUnit(
        unit_id=unit_id,
        title=title,
        target_start=start,
        target_end=start + len(text),
        input_text=text,
        source_spans=[
            SourceSpan(
                source_id="S" + unit_id,
                start_char=start,
                end_char=start + len(text),
            )
        ],
        input_fingerprint=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        target_chars=max(1, int(len(text) * 0.15)),
    )


def _gold_units() -> list[DistillUnit]:
    background = "历史背景与行业概述，介绍过去的发展过程。" * 12
    prerequisite = "关键概念的定义与必要前提：先理解容量约束，后续机制才成立。" * 12
    mechanism = "核心机制与执行步骤：按优先级分配容量，并根据反馈调整规则。" * 12
    duplicate = "核心机制与执行步骤：按优先级分配容量，并根据反馈调整规则。" * 12
    texts = [background, prerequisite, mechanism, duplicate]
    titles = ["背景", "必要前提", "核心机制", "重复回顾"]
    units: list[DistillUnit] = []
    cursor = 0
    for index, (title, text) in enumerate(zip(titles, texts), start=1):
        units.append(_unit(f"U{index}", title, text, cursor))
        cursor += len(text)
    return units


def test_orientation_scan_persists_all_four_factors_and_reasons() -> None:
    scan = scan_orientation(_gold_units(), book_type="general")

    assert len(scan.scores) == 4
    for score in scan.scores:
        assert 0 <= score.factors.relevance <= 1
        assert 0 <= score.factors.novelty <= 1
        assert 0 <= score.factors.dependency <= 1
        assert 0 <= score.factors.evidence_value <= 1
        assert score.reason
    by_id = {score.unit_id: score for score in scan.scores}
    assert by_id["U4"].factors.novelty < by_id["U3"].factors.novelty


def test_key_mechanism_and_prerequisite_get_more_budget_density() -> None:
    units = _gold_units()
    scan = scan_orientation(units, book_type="general")
    plan = allocate_budget(
        units,
        scan,
        book_id="book123",
        book_type="general",
        strength="standard",
    )
    by_id = {allocation.unit_id: allocation for allocation in plan.allocations}
    lengths = {unit.unit_id: len(unit.input_text) for unit in units}
    density = {
        unit_id: allocation.target_chars / lengths[unit_id]
        for unit_id, allocation in by_id.items()
    }

    assert density["U3"] > density["U1"]
    assert density["U2"] > density["U1"]
    assert density["U3"] > density["U4"]
    assert plan.total_target_chars == round(plan.total_source_chars * 0.15)


def test_budget_is_deterministic_and_applied_to_units() -> None:
    units = _gold_units()
    first_scan = scan_orientation(units, book_type="technical")
    second_scan = scan_orientation(units, book_type="technical")
    first = allocate_budget(
        units,
        first_scan,
        book_id="book123",
        book_type="technical",
        strength="standard",
    )
    second = allocate_budget(
        units,
        second_scan,
        book_id="book123",
        book_type="technical",
        strength="standard",
    )

    assert first.model_dump(mode="json") == second.model_dump(mode="json")
    updated = apply_budget_plan(units, first)
    targets = {allocation.unit_id: allocation.target_chars for allocation in first.allocations}
    assert all(unit.target_chars == targets[unit.unit_id] for unit in updated)


def test_strength_and_book_type_create_distinct_budget_plans() -> None:
    units = _gold_units()
    general_scan = scan_orientation(units, book_type="general")
    technical_scan = scan_orientation(units, book_type="technical")
    standard = allocate_budget(
        units,
        general_scan,
        book_id="book123",
        book_type="general",
        strength="standard",
    )
    conservative = allocate_budget(
        units,
        general_scan,
        book_id="book123",
        book_type="general",
        strength="conservative",
    )
    technical = allocate_budget(
        units,
        technical_scan,
        book_id="book123",
        book_type="technical",
        strength="standard",
    )

    assert conservative.total_target_chars > standard.total_target_chars
    assert technical.total_target_chars > standard.total_target_chars
    assert technical.allocations != standard.allocations

