import asyncio
from pathlib import Path

import pytest

from app.core.execution import DistillCancelled, TaskExecutionContext
from app.core.task_store import TaskStore
from app.models.domain import (
    DistillUnit,
    KnowledgeKind,
    KnowledgeUnit,
    SourceAnchor,
    SourceSpan,
    VerificationStatus,
)


def _unit() -> DistillUnit:
    return DistillUnit(
        unit_id="U1",
        title="第一章",
        target_start=0,
        target_end=10,
        input_text="abcdefghij",
        source_spans=[SourceSpan(source_id="S1", start_char=0, end_char=10)],
        input_fingerprint="a" * 64,
        target_chars=2,
    )


def _knowledge() -> KnowledgeUnit:
    return KnowledgeUnit(
        knowledge_id="K1",
        kind=KnowledgeKind.SOURCE_CLAIM,
        content="ab",
        anchors=[
            SourceAnchor.from_text(
                source_id="S1", start_char=0, end_char=2, quote="ab"
            )
        ],
        verification_status=VerificationStatus.VERIFIED,
        origin_unit_ids=["U1"],
    )


def _context(store: TaskStore, task_id: str, **overrides) -> TaskExecutionContext:
    values = {
        "store": store,
        "task_id": task_id,
        "source_fingerprint": "f" * 64,
        "prune_config_hash": "p" * 64,
        "book_type": "general",
        "strength": "standard",
        "model": "deepseek-v4-flash",
        "prompt_version": "1.0",
    }
    values.update(overrides)
    return TaskExecutionContext(**values)


def test_cache_key_includes_every_required_invalidation_dimension(tmp_path: Path) -> None:
    store = TaskStore(tmp_path / "tasks.db")
    task = store.create_task("book", "general", "standard")
    base = _context(store, task.task_id)
    base_key = base.cache_key(_unit())

    variants = [
        _context(store, task.task_id, source_fingerprint="e" * 64),
        _context(store, task.task_id, prune_config_hash="q" * 64),
        _context(store, task.task_id, book_type="technical"),
        _context(store, task.task_id, strength="conservative"),
        _context(store, task.task_id, model="another-model"),
        _context(store, task.task_id, prompt_version="2.0"),
    ]

    assert all(context.cache_key(_unit()) != base_key for context in variants)


def test_completed_unit_is_reused_by_a_new_task(tmp_path: Path) -> None:
    store = TaskStore(tmp_path / "tasks.db")
    first_task = store.create_task("book", "general", "standard")
    first = _context(store, first_task.task_id)
    first.start_unit(_unit())
    first.complete_unit(_unit(), [_knowledge()])

    second_task = store.create_task("book", "general", "standard")
    second = _context(store, second_task.task_id)
    cached = second.load_cached(_unit())

    assert cached is not None
    assert cached[0].knowledge_id == "K1"
    assert second.cache_hits == 1
    assert store.get_units(second_task.task_id)[0].status == "done"


def test_cancelled_context_raises_before_work_starts(tmp_path: Path) -> None:
    store = TaskStore(tmp_path / "tasks.db")
    task = store.create_task("book", "general", "standard")
    context = _context(store, task.task_id)
    store.request_cancel(task.task_id)

    with pytest.raises(DistillCancelled):
        context.raise_if_cancelled()


def test_failed_checkpoint_is_not_cached(tmp_path: Path) -> None:
    store = TaskStore(tmp_path / "tasks.db")
    task = store.create_task("book", "general", "standard")
    context = _context(store, task.task_id)
    context.start_unit(_unit())
    context.fail_unit(_unit(), "timeout")

    assert context.load_cached(_unit()) is None
    checkpoint = store.get_units(task.task_id)[0]
    assert checkpoint.status == "error"
    assert checkpoint.error == "timeout"

