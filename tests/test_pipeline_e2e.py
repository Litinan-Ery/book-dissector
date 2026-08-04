import asyncio
import json
from pathlib import Path

from app import config
from app.core.extractors.base import extract_book
from app.core.pipeline import run_pipeline
from app.core.task_store import TaskStore


def test_one_pipeline_call_prunes_distills_validates_and_exports(
    tmp_path: Path, monkeypatch
) -> None:
    storage = tmp_path / "storage"
    books = storage / "books"
    intermediate = storage / "intermediate"
    output = storage / "output"
    runs = storage / "runs"
    for path in (books, intermediate, output, runs):
        path.mkdir(parents=True)
    monkeypatch.setattr(config, "STORAGE_DIR", storage)
    monkeypatch.setattr(config, "BOOKS_DIR", books)
    monkeypatch.setattr(config, "INTERMEDIATE_DIR", intermediate)
    monkeypatch.setattr(config, "OUTPUT_DIR", output)
    monkeypatch.setattr(config, "RUNS_DIR", runs)
    monkeypatch.setattr(config, "get_api_key", lambda: "test-key")

    book_id = "book123"
    source = books / f"{book_id}_fixture.md"
    source.write_text(
        "# 第一章\n\n准确性是效率的前提。作者给出完整证据与适用边界。\n",
        encoding="utf-8",
    )
    assert extract_book(book_id, source).ok
    store = TaskStore(storage / "tasks.db")
    task = store.create_task(book_id, "general", "standard")

    draft = asyncio.run(
        run_pipeline(
            book_id,
            book_type="general",
            strength="standard",
            task_store=store,
            task_id=task.task_id,
            use_fake=True,
        )
    )

    restored = store.get_task(task.task_id)
    assert restored is not None
    assert restored.status == "done"
    assert restored.run_id == draft.run_id
    assert draft.quality_status == "pass"
    assert restored.metrics["input_tokens"] > 0
    assert restored.metrics["output_tokens"] > 0
    assert restored.metrics["actual_cost_cny"] >= 0
    assert "time_estimate_error" in restored.metrics
    assert Path(draft.output_path).exists()
    run_dir = runs / draft.run_id
    assert (run_dir / "manifest.json").exists()
    assert (run_dir / "prune" / "result.json").exists()
    assert (run_dir / "distill" / "result.json").exists()
    assert (run_dir / "quality" / "report.json").exists()
    assert (run_dir / "export" / "result.md").exists()
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "completed"
    assert manifest["current_stage"] == "export"


def test_each_pipeline_run_gets_an_independent_version_directory(
    tmp_path: Path, monkeypatch
) -> None:
    storage = tmp_path / "storage"
    for name in ("books", "intermediate", "output", "runs"):
        (storage / name).mkdir(parents=True)
    monkeypatch.setattr(config, "STORAGE_DIR", storage)
    monkeypatch.setattr(config, "BOOKS_DIR", storage / "books")
    monkeypatch.setattr(config, "INTERMEDIATE_DIR", storage / "intermediate")
    monkeypatch.setattr(config, "OUTPUT_DIR", storage / "output")
    monkeypatch.setattr(config, "RUNS_DIR", storage / "runs")
    monkeypatch.setattr(config, "get_api_key", lambda: "test-key")
    book_id = "book123"
    source = config.BOOKS_DIR / f"{book_id}_fixture.md"
    source.write_text("# 第一章\n\n核心正文与证据。\n", encoding="utf-8")
    assert extract_book(book_id, source).ok

    first = asyncio.run(run_pipeline(book_id, use_fake=True))
    second = asyncio.run(run_pipeline(book_id, use_fake=True))

    assert first.run_id != second.run_id
    assert (config.RUNS_DIR / first.run_id / "manifest.json").exists()
    assert (config.RUNS_DIR / second.run_id / "manifest.json").exists()
