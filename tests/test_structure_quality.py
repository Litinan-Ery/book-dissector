from app.core.extractors.base import Chapter
from app.core.quality import validate_structure


def test_consecutive_chapters_pass_with_full_body_coverage() -> None:
    report = validate_structure(
        "x" * 100,
        [Chapter("第一章", 1, 0, 40), Chapter("第二章", 1, 40, 100)],
    )

    assert report.valid is True
    assert report.body_coverage == 1.0
    assert report.uncovered_ranges == []
    assert report.duplicate_ranges == []


def test_gap_is_reported_and_fails_coverage_gate() -> None:
    report = validate_structure(
        "x" * 100,
        [Chapter("第一章", 1, 0, 40), Chapter("第二章", 1, 50, 100)],
    )

    assert report.valid is False
    assert report.body_coverage == 0.9
    assert [(item.start, item.end) for item in report.uncovered_ranges] == [(40, 50)]
    assert any(issue.code == "coverage_below_threshold" for issue in report.issues)


def test_overlap_and_zero_length_chapter_are_blocking() -> None:
    report = validate_structure(
        "x" * 100,
        [
            Chapter("第一章", 1, 0, 60),
            Chapter("第二章", 1, 50, 100),
            Chapter("空章", 1, 100, 100),
        ],
    )

    assert report.valid is False
    assert [(item.start, item.end) for item in report.duplicate_ranges] == [(50, 60)]
    assert {issue.code for issue in report.issues} >= {"overlap", "empty_chapter"}


def test_out_of_order_and_out_of_bounds_chapters_fail() -> None:
    report = validate_structure(
        "x" * 100,
        [Chapter("后章", 1, 50, 120), Chapter("前章", 1, 10, 50)],
    )

    assert report.valid is False
    assert {issue.code for issue in report.issues} >= {"out_of_order", "out_of_bounds"}


def test_document_without_explicit_chapters_uses_one_implicit_unit() -> None:
    report = validate_structure("无显式章节的正文", [])

    assert report.valid is True
    assert report.body_coverage == 1.0
    assert any(issue.code == "implicit_document" and not issue.blocking for issue in report.issues)

