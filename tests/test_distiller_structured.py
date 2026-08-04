import asyncio
import hashlib
import json
from pathlib import Path

import pytest

from app import config
from app.api.tasks import _completion_status, _persist, _to_out
from app.core.distiller import QualityGateError, distill_book
from app.core.execution import DistillCancelled, TaskExecutionContext
from app.core.spanmap import build_span_map
from app.core.task_store import TaskStore
from app.models.domain import QualityStatus


def _write_book(tmp_path: Path, monkeypatch, *, structure_valid: bool = True) -> tuple[str, str]:
    books = tmp_path / "books"
    intermediate = tmp_path / "intermediate"
    output = tmp_path / "output"
    runs = tmp_path / "runs"
    for path in (books, intermediate, output, runs):
        path.mkdir(parents=True)
    monkeypatch.setattr(config, "BOOKS_DIR", books)
    monkeypatch.setattr(config, "INTERMEDIATE_DIR", intermediate)
    monkeypatch.setattr(config, "OUTPUT_DIR", output)
    monkeypatch.setattr(config, "RUNS_DIR", runs)
    monkeypatch.setattr(config, "get_api_key", lambda: "test-key")

    book_id = "book123"
    text = "第一章\n作者明确主张效率必须建立在准确性之上。随后给出实验数据和适用边界。"
    fingerprint = hashlib.sha256(text.encode("utf-8")).hexdigest()
    (books / f"{book_id}.txt").write_text(text, encoding="utf-8")
    (books / f"{book_id}.meta.json").write_text(
        json.dumps(
            {
                "title": "夹具书",
                "source_fingerprint": fingerprint,
                "extract_status": "ok",
                "chapters": [
                    {"title": "第一章", "level": 1, "start_char": 0, "end_char": len(text)}
                ],
                "structure_report": {
                    "valid": structure_valid,
                    "body_start": 0,
                    "body_end": len(text),
                    "body_coverage": 1.0 if structure_valid else 0.5,
                    "uncovered_ranges": [],
                    "duplicate_ranges": [],
                    "issues": [],
                },
                "modality_warnings": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    mapping = build_span_map(len(text), [])
    (intermediate / f"{book_id}.pruned.txt").write_text(text, encoding="utf-8")
    (intermediate / f"{book_id}.prune.json").write_text(
        json.dumps(
            {
                "pruned_chapters": [
                    {"title": "第一章", "level": 1, "start_char": 0, "end_char": len(text)}
                ],
                "span_map": [entry.model_dump(mode="json") for entry in mapping],
                "span_map_report": {
                    "valid": True,
                    "source_coverage": 1.0,
                    "target_coverage": 1.0,
                    "issues": [],
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return book_id, text


def test_fake_distillation_produces_only_anchored_knowledge_units(
    tmp_path: Path, monkeypatch
) -> None:
    book_id, original = _write_book(tmp_path, monkeypatch)

    result = asyncio.run(distill_book(book_id, use_fake=True))

    assert result.knowledge_units
    assert result.anchor_coverage == 1.0
    assert result.unit_coverage.coverage == 1.0
    assert result.orientation_scan is not None
    assert result.budget_plan is not None
    assert sum(unit.target_chars for unit in result.distill_units) == result.budget_plan.total_target_chars
    assert result.quality_report.status == QualityStatus.PASS
    assert _completion_status(result) == "done"
    assert result.api_calls == len(result.distill_units)
    for knowledge in result.knowledge_units:
        assert knowledge.anchors
        for anchor in knowledge.anchors:
            assert original[anchor.start_char : anchor.end_char] == anchor.quote
    assert "来源" in result.final_text

    public = _to_out(result)
    _persist(book_id, result)
    persisted = json.loads(
        (config.INTERMEDIATE_DIR / f"{book_id}.distill.json").read_text(encoding="utf-8")
    )
    assert public.anchor_coverage == 1.0
    assert public.knowledge_units
    assert persisted["unit_coverage"]["coverage"] == 1.0
    assert persisted["knowledge_units"][0]["anchors"]
    assert persisted["distill_units"][0]["source_spans"]
    assert persisted["orientation_scan"]["scores"]
    assert persisted["budget_plan"]["allocations"]
    quality = json.loads(
        (config.INTERMEDIATE_DIR / f"{book_id}.quality.json").read_text(encoding="utf-8")
    )
    assert quality["status"] == "pass"


def test_structure_gate_blocks_model_calls_before_distillation(
    tmp_path: Path, monkeypatch
) -> None:
    book_id, _ = _write_book(tmp_path, monkeypatch, structure_valid=False)

    with pytest.raises(QualityGateError, match="结构"):
        asyncio.run(distill_book(book_id, use_fake=True))


def test_quality_failure_is_not_reported_as_done(tmp_path: Path, monkeypatch) -> None:
    book_id, _ = _write_book(tmp_path, monkeypatch)
    result = asyncio.run(distill_book(book_id, use_fake=True))
    result.quality_report = result.quality_report.model_copy(
        update={"status": QualityStatus.FAIL, "blocking_issues": ["未解析图片"]}
    )

    assert _completion_status(result) == "quality_failed"


def test_second_identical_run_reuses_unit_cache_without_api_calls(
    tmp_path: Path, monkeypatch
) -> None:
    book_id, _ = _write_book(tmp_path, monkeypatch)
    store = TaskStore(tmp_path / "tasks.db")
    first_task = store.create_task(book_id, "general", "standard")
    common = {
        "store": store,
        "source_fingerprint": hashlib.sha256(
            (config.BOOKS_DIR / f"{book_id}.txt").read_bytes()
        ).hexdigest(),
        "prune_config_hash": "p" * 64,
        "book_type": "general",
        "strength": "standard",
        "model": config.DEEPSEEK_MODEL,
        "prompt_version": "1.0",
    }
    first_context = TaskExecutionContext(task_id=first_task.task_id, **common)
    first = asyncio.run(
        distill_book(book_id, use_fake=True, execution=first_context)
    )
    second_task = store.create_task(book_id, "general", "standard")
    second_context = TaskExecutionContext(task_id=second_task.task_id, **common)
    second = asyncio.run(
        distill_book(book_id, use_fake=True, execution=second_context)
    )

    assert first.api_calls == len(first.distill_units)
    assert second.api_calls == 0
    assert second.cache_hits == len(second.distill_units)
    assert second.final_text == first.final_text


def test_cancelled_task_stops_before_first_model_request(
    tmp_path: Path, monkeypatch
) -> None:
    book_id, _ = _write_book(tmp_path, monkeypatch)
    store = TaskStore(tmp_path / "tasks.db")
    task = store.create_task(book_id, "general", "standard")
    store.request_cancel(task.task_id)
    context = TaskExecutionContext(
        store=store,
        task_id=task.task_id,
        source_fingerprint="f" * 64,
        prune_config_hash="p" * 64,
        book_type="general",
        strength="standard",
        model=config.DEEPSEEK_MODEL,
        prompt_version="1.0",
    )

    with pytest.raises(DistillCancelled):
        asyncio.run(distill_book(book_id, use_fake=True, execution=context))
