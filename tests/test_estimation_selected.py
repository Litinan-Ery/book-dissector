from app.core.distiller import build_distill_units, count_distill_calls
from app.core.estimation import estimate_request
from app.core.extractors.base import Chapter


def test_estimate_exposes_tokens_calls_time_and_cost() -> None:
    estimate = estimate_request(
        "中文正文" * 5000,
        target_ratio=0.15,
        max_chunk_chars=12000,
    )

    assert estimate["input_tokens"] > 0
    assert estimate["output_tokens"] > 0
    assert estimate["api_calls"] >= 1
    assert estimate["time_seconds_low"] < estimate["time_seconds_high"]
    assert estimate["cost_cny_low"] <= estimate["cost_cny_high"]
    assert estimate["pricing_source"].startswith("https://api-docs.deepseek.com/")


def test_distill_units_keep_nested_sections_inside_top_level_chapters() -> None:
    text = "# 第一章\n导言\n## 第一节\n细节\n# 第二章\n结论"
    second_section = text.index("## 第一节")
    second_chapter = text.index("# 第二章")
    chapters = [
        Chapter("第一章", 1, 0, second_section),
        Chapter("第一节", 2, second_section, second_chapter),
        Chapter("第二章", 1, second_chapter, len(text)),
    ]

    units = build_distill_units(text, chapters)

    assert [title for title, _segment in units] == ["第一章", "第二章"]
    assert "## 第一节" in units[0][1]
    assert count_distill_calls(text, chapters) == 2


def test_estimate_can_use_real_planned_call_count() -> None:
    estimate = estimate_request(
        "正文" * 100,
        target_ratio=0.15,
        max_chunk_chars=12000,
        api_calls=7,
    )

    assert estimate["api_calls"] == 7
    assert estimate["time_seconds_low"] == 14
    assert estimate["time_seconds_high"] == 56
