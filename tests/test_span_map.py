from app.core.pruner import Region, prune
from app.core.spanmap import build_span_map, validate_span_map
from app.models.domain import SpanMapStatus


def test_span_map_is_contiguous_and_reproduces_pruned_text() -> None:
    source = "abcdefghij"
    regions = [
        Region(2, 4, "copyright", "cd"),
        Region(7, 8, "duplicate", "h"),
    ]

    mapping = build_span_map(len(source), regions)
    target = "".join(
        source[entry.source_start : entry.source_end]
        for entry in mapping
        if entry.status == SpanMapStatus.KEPT
    )
    report = validate_span_map(source, target, mapping)

    assert report.valid is True
    assert report.source_coverage == 1.0
    assert report.target_coverage == 1.0
    assert target == "abefgij"
    assert [(entry.status.value, entry.source_start, entry.source_end) for entry in mapping] == [
        ("kept", 0, 2),
        ("deleted", 2, 4),
        ("kept", 4, 7),
        ("deleted", 7, 8),
        ("kept", 8, 10),
    ]


def test_prune_result_contains_a_valid_complete_span_map() -> None:
    text = "版权信息 ISBN 123\n\n第一章\n正文内容。\n"

    result = prune(text)
    report = validate_span_map(text, result.pruned_text, result.span_map)

    assert report.valid is True
    assert result.span_map_report.valid is True
    assert result.span_map_report.source_coverage == 1.0
    assert result.span_map_report.target_coverage == 1.0
    assert result.span_map
    assert any(entry.status == SpanMapStatus.DELETED for entry in result.span_map)


def test_duplicate_detection_keeps_one_source_occurrence() -> None:
    repeated = "这是必须保留的共同知识重复段落。"
    text = f"第一章\n{repeated}\n甲。\n{repeated}\n乙。\n{repeated}\n"

    result = prune(text)

    assert result.pruned_text.count(repeated) == 1
    assert sum(region.reason == "duplicate" for region in result.regions) == 2


def test_backmatter_is_retained_as_evidence_instead_of_blanket_deleted() -> None:
    evidence = "[1] 关键实验数据与限制条件。"
    text = f"第一章\n正文观点。\n\n参考文献\n{evidence}\n"

    result = prune(text)

    assert evidence in result.pruned_text
    assert not any(region.reason == "backmatter" for region in result.regions)
    assert any(region.reason == "backmatter_evidence" for region in result.evidence_regions)


def test_invalid_mapping_detects_source_gap() -> None:
    source = "abcdefghij"
    mapping = build_span_map(len(source), [Region(2, 4, "copyright", "cd")])
    mapping.pop(0)

    report = validate_span_map(source, "efghij", mapping)

    assert report.valid is False
    assert any("source" in issue for issue in report.issues)
