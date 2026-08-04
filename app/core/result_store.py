"""提炼结果、审计清单与质量报告的原子持久化。"""
from __future__ import annotations

from .. import config
from .atomic import atomic_write_json, atomic_write_text


def distill_result_payload(result) -> dict:
    kept_ratio = (
        result.total_output_chars / result.total_source_chars
        if result.total_source_chars
        else 0.0
    )
    return {
        "book_title": result.book_title,
        "book_type": result.book_type,
        "strength": result.strength,
        "total_source_chars": result.total_source_chars,
        "total_output_chars": result.total_output_chars,
        "final_text": result.final_text,
        "kept_ratio": round(kept_ratio, 6),
        "api_calls": result.api_calls,
        "cache_hits": result.cache_hits,
        "input_tokens": result.input_tokens,
        "output_tokens": result.output_tokens,
        "prompt_cache_hit_tokens": result.prompt_cache_hit_tokens,
        "prompt_cache_miss_tokens": result.prompt_cache_miss_tokens,
        "actual_cost_cny": result.actual_cost_cny,
        "errors": result.errors,
        "anchor_coverage": result.anchor_coverage,
        "unit_coverage": result.unit_coverage.model_dump(mode="json"),
        "distill_units": [
            unit.model_dump(mode="json") for unit in result.distill_units
        ],
        "knowledge_units": [
            unit.model_dump(mode="json") for unit in result.knowledge_units
        ],
        "duplicate_merged_count": result.duplicate_merged_count,
        "quality_report": result.quality_report.model_dump(mode="json"),
        "model": config.DEEPSEEK_MODEL,
        "prompt_version": "1.0",
        "orientation_scan": (
            result.orientation_scan.model_dump(mode="json")
            if result.orientation_scan
            else None
        ),
        "budget_plan": (
            result.budget_plan.model_dump(mode="json")
            if result.budget_plan
            else None
        ),
        "chapters": [
            {
                "title": chapter.title,
                "source_chars": chapter.source_chars,
                "target_chars": chapter.target_chars,
                "output_chars": chapter.output_chars,
                "error": chapter.error,
                "unit_id": chapter.unit_id,
            }
            for chapter in result.chapters
        ],
    }


def persist_distill_result(book_id: str, result) -> dict:
    config.ensure_dirs()
    payload = distill_result_payload(result)
    atomic_write_text(
        config.INTERMEDIATE_DIR / f"{book_id}.distilled.md",
        result.final_text,
    )
    atomic_write_json(
        config.INTERMEDIATE_DIR / f"{book_id}.distill.json", payload
    )
    atomic_write_json(
        config.INTERMEDIATE_DIR / f"{book_id}.quality.json",
        result.quality_report,
    )
    return payload
