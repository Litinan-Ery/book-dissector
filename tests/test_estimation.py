from app.core.estimation import actual_cost_cny, estimate_cost_cny, estimate_request


def test_deepseek_v4_flash_cost_range_uses_cache_hit_to_peak_miss_bounds() -> None:
    cost = estimate_cost_cny(input_tokens=1_000_000, output_tokens=1_000_000)

    assert cost["low_cny"] == 2.02
    assert cost["high_cny"] == 6.0
    assert cost["pricing_version"] == "2026-08-05"


def test_request_estimate_exposes_tokens_calls_time_and_cost() -> None:
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


def test_actual_cost_uses_reported_hit_miss_and_output_tokens() -> None:
    assert actual_cost_cny(
        cache_hit_tokens=1_000_000,
        cache_miss_tokens=1_000_000,
        output_tokens=1_000_000,
    ) == 3.02
