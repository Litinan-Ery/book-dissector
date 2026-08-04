from app.core.extractors.base import Chapter
from app.core.spanmap import build_span_map
from app.core.units import (
    build_distill_units,
    source_spans_for_target,
    validate_unit_coverage,
)
from app.core.pruner import Region


def test_target_range_maps_back_to_each_kept_original_span() -> None:
    source = "abcDELETEdef"
    mapping = build_span_map(len(source), [Region(3, 9, "copyright", "DELETE")])

    spans = source_spans_for_target(0, 6, mapping, "e" * 64)

    assert [(span.start_char, span.end_char) for span in spans] == [(0, 3), (9, 12)]
    assert "".join(source[span.start_char : span.end_char] for span in spans) == "abcdef"
    assert len({span.source_id for span in spans}) == 2


def test_build_units_preserves_chapter_boundaries_and_full_coverage() -> None:
    text = "第一章\nabcdefghij\n第二章\nklmnopqrst"
    second_start = text.index("第二章")
    chapters = [
        Chapter("第一章", 1, 0, second_start),
        Chapter("第二章", 1, second_start, len(text)),
    ]
    mapping = build_span_map(len(text), [])

    units = build_distill_units(
        text=text,
        chapters=chapters,
        span_map=mapping,
        source_fingerprint="f" * 64,
        max_chars=12,
        overlap_chars=2,
        target_ratio=0.2,
    )
    report = validate_unit_coverage(units, body_start=0, body_end=len(text))

    assert report.coverage == 1.0
    assert report.uncovered_ranges == []
    assert all(unit.target_start < unit.target_end for unit in units)
    assert all(unit.input_text == text[unit.target_start : unit.target_end] for unit in units)
    assert {unit.title for unit in units} == {"第一章", "第二章"}
    assert any(
        left.target_end > right.target_start
        for left, right in zip(units, units[1:])
        if left.title == right.title
    )


def test_unit_ids_and_input_fingerprints_are_stable() -> None:
    text = "无章节正文" * 10
    mapping = build_span_map(len(text), [])
    kwargs = {
        "text": text,
        "chapters": [],
        "span_map": mapping,
        "source_fingerprint": "a" * 64,
        "max_chars": 20,
        "overlap_chars": 3,
        "target_ratio": 0.15,
    }

    first = build_distill_units(**kwargs)
    second = build_distill_units(**kwargs)

    assert [unit.unit_id for unit in first] == [unit.unit_id for unit in second]
    assert [unit.input_fingerprint for unit in first] == [
        unit.input_fingerprint for unit in second
    ]


def test_unit_coverage_reports_a_real_gap() -> None:
    text = "abcdefghij"
    mapping = build_span_map(len(text), [])
    units = build_distill_units(
        text=text,
        chapters=[Chapter("局部", 1, 0, 4)],
        span_map=mapping,
        source_fingerprint="a" * 64,
        max_chars=20,
        overlap_chars=0,
        target_ratio=0.2,
    )

    report = validate_unit_coverage(units, body_start=0, body_end=len(text))

    assert report.coverage == 0.4
    assert [(item.start, item.end) for item in report.uncovered_ranges] == [(4, 10)]

