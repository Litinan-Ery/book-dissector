"""开始前的 token、调用量、时间与 DeepSeek V4 Flash 费用区间估算。"""
from __future__ import annotations

import math
import re

PRICING_VERSION = "2026-08-05"
PRICING_SOURCE = "https://api-docs.deepseek.com/zh-cn/quick_start/pricing/"

# 官方 2026-08-05 页面：百万元 token 的人民币价格。
INPUT_CACHE_HIT_CNY_PER_M = 0.02
INPUT_CACHE_MISS_CNY_PER_M = 1.0
OUTPUT_CNY_PER_M = 2.0
PEAK_MULTIPLIER = 2.0


def estimate_text_tokens(text: str) -> int:
    cjk = len(re.findall(r"[\u3400-\u9fff]", text))
    other = len(re.sub(r"[\s\u3400-\u9fff]", "", text))
    return max(1, cjk + math.ceil(other / 4)) if text else 0


def estimate_cost_cny(*, input_tokens: int, output_tokens: int) -> dict:
    low = (
        input_tokens * INPUT_CACHE_HIT_CNY_PER_M
        + output_tokens * OUTPUT_CNY_PER_M
    ) / 1_000_000
    high = (
        input_tokens * INPUT_CACHE_MISS_CNY_PER_M
        + output_tokens * OUTPUT_CNY_PER_M
    ) * PEAK_MULTIPLIER / 1_000_000
    return {
        "low_cny": round(low, 6),
        "high_cny": round(high, 6),
        "pricing_version": PRICING_VERSION,
        "pricing_source": PRICING_SOURCE,
    }


def actual_cost_cny(
    *, cache_hit_tokens: int, cache_miss_tokens: int, output_tokens: int
) -> float:
    value = (
        cache_hit_tokens * INPUT_CACHE_HIT_CNY_PER_M
        + cache_miss_tokens * INPUT_CACHE_MISS_CNY_PER_M
        + output_tokens * OUTPUT_CNY_PER_M
    ) / 1_000_000
    return round(value, 6)


def estimate_request(
    text: str,
    *,
    target_ratio: float,
    max_chunk_chars: int,
) -> dict:
    if max_chunk_chars <= 0:
        raise ValueError("max_chunk_chars must be positive")
    calls = max(1, math.ceil(len(text) / max_chunk_chars)) if text else 0
    source_tokens = estimate_text_tokens(text)
    # 来源标签、JSON schema 与系统提示造成输入开销；每次调用再预留约 300 tokens。
    input_tokens = math.ceil(source_tokens * 1.12) + calls * 300
    output_tokens = math.ceil(source_tokens * target_ratio)
    cost = estimate_cost_cny(
        input_tokens=input_tokens, output_tokens=output_tokens
    )
    return {
        "source_chars": len(text),
        "api_calls": calls,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "time_seconds_low": calls * 2,
        "time_seconds_high": calls * 8,
        "cost_cny_low": cost["low_cny"],
        "cost_cny_high": cost["high_cny"],
        "pricing_version": cost["pricing_version"],
        "pricing_source": cost["pricing_source"],
    }
