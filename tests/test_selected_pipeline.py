import asyncio
from pathlib import Path

from app import config
from app.core.extractors.base import extract_book
from app.core.pipeline import run_pipeline
from app.core.task_store import TaskStore


def test_one_click_pipeline_exports_and_uses_cache(tmp_path: Path, monkeypatch) -> None:
    storage = tmp_path / "storage"
    for name in ("books", "intermediate", "output"):
        (storage / name).mkdir(parents=True)
    monkeypatch.setattr(config, "STORAGE_DIR", storage)
    monkeypatch.setattr(config, "BOOKS_DIR", storage / "books")
    monkeypatch.setattr(config, "INTERMEDIATE_DIR", storage / "intermediate")
    monkeypatch.setattr(config, "OUTPUT_DIR", storage / "output")
    monkeypatch.setattr(config, "TASK_DB", storage / "tasks.db")
    monkeypatch.setattr(config, "get_api_key", lambda: "test-key")

    book_id = "book1"
    source = config.BOOKS_DIR / f"{book_id}_source.md"
    source.write_text("# 第一章\n\n核心观点和生动例子。\n", encoding="utf-8")
    assert extract_book(book_id, source).ok
    store = TaskStore(config.TASK_DB)
    first_task = store.create_task(book_id, "general", "standard")
    first = asyncio.run(
        run_pipeline(
            book_id,
            task_store=store,
            task_id=first_task.task_id,
            use_fake=True,
        )
    )
    assert Path(first.output_path).exists()
    assert store.get_task(first_task.task_id).status == "done"

    second_task = store.create_task(book_id, "general", "standard")
    asyncio.run(
        run_pipeline(
            book_id,
            task_store=store,
            task_id=second_task.task_id,
            use_fake=True,
        )
    )
    assert store.get_task(second_task.task_id).metrics["cache_hits"] >= 1
