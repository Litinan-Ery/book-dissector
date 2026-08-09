from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from app import config
from app.core import distiller
from app.core.distiller import DistillCancelled
from app.core.task_store import TaskStore


def test_delete_during_model_call_prevents_length_retry(
    tmp_path: Path, monkeypatch
) -> None:
    storage = tmp_path / "storage"
    books = storage / "books"
    intermediate = storage / "intermediate"
    output = storage / "output"
    for path in (books, intermediate, output):
        path.mkdir(parents=True)
    monkeypatch.setattr(config, "STORAGE_DIR", storage)
    monkeypatch.setattr(config, "BOOKS_DIR", books)
    monkeypatch.setattr(config, "INTERMEDIATE_DIR", intermediate)
    monkeypatch.setattr(config, "OUTPUT_DIR", output)
    monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr(config, "DEEPSEEK_MODEL", "test-model")
    config.set_api_key("test-key")
    book_id = "book1"
    (books / f"{book_id}.txt").write_text("正文" * 1000, encoding="utf-8")
    (books / f"{book_id}.meta.json").write_text(
        json.dumps({"title": "边界测试", "chapters": []}),
        encoding="utf-8",
    )
    store = TaskStore(storage / "tasks.db")
    task = store.create_task(book_id, "general", "standard")
    store.update_task(task.task_id, status="running")
    calls = 0

    async def model_call(_system: str, _user: str, _api_key: str) -> str:
        nonlocal calls
        calls += 1
        # Simulate the user deleting the running task while this request is
        # in flight. The deliberately short result would normally trigger a
        # second length-correction request.
        assert store.request_task_delete(task.task_id) == "deleting"
        return "过短"

    monkeypatch.setattr(distiller, "_call_deepseek", model_call)

    with pytest.raises(DistillCancelled):
        asyncio.run(
            distiller.distill_book(
                book_id,
                task_store=store,
                task_id=task.task_id,
                use_fake=False,
            )
        )

    assert calls == 1
    deleting = store.get_task(task.task_id)
    assert deleting is not None
    assert deleting.delete_requested is True
