from __future__ import annotations

import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import config
from app.api import books, tasks
from app.core import library_cleanup
from app.core.library_cleanup import delete_book, recover_incomplete_book_deletions
from app.core.task_store import (
    ActiveBookTasksError,
    BookDeletionInProgressError,
    TaskStore,
)
from app.main import app


def _configure(tmp_path: Path, monkeypatch) -> TaskStore:
    storage = tmp_path / "storage"
    monkeypatch.setattr(config, "STORAGE_DIR", storage)
    monkeypatch.setattr(config, "BOOKS_DIR", storage / "books")
    monkeypatch.setattr(config, "INTERMEDIATE_DIR", storage / "intermediate")
    monkeypatch.setattr(config, "OUTPUT_DIR", storage / "output")
    monkeypatch.setattr(config, "TASK_DB", storage / "tasks.db")
    monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "config.json")
    config.ensure_dirs()
    tasks.reset_runtime_for_tests()
    monkeypatch.setattr(tasks, "_schedule_pending", lambda: None)
    return tasks._get_store()


def _write_book(book_id: str, *, status: str = "ok") -> list[Path]:
    paths = [
        config.BOOKS_DIR / f"{book_id}_sample.txt",
        config.BOOKS_DIR / f"{book_id}.txt",
        config.BOOKS_DIR / f"{book_id}.meta.json",
        config.INTERMEDIATE_DIR / f"{book_id}.pruned.txt",
    ]
    paths[0].write_text("import copy", encoding="utf-8")
    paths[1].write_text("正文", encoding="utf-8")
    paths[2].write_text(
        json.dumps({"book_id": book_id, "title": "可删除的书", "extract_status": status}),
        encoding="utf-8",
    )
    paths[3].write_text("中间稿", encoding="utf-8")
    return paths


def test_book_delete_cleans_owned_data_but_preserves_external_outputs(
    tmp_path: Path, monkeypatch
) -> None:
    store = _configure(tmp_path, monkeypatch)
    book_id = "book1"
    managed = _write_book(book_id)
    external_original = tmp_path / "source-original.txt"
    external_original.write_text("owner copy", encoding="utf-8")
    output = config.OUTPUT_DIR / "book1-精华.md"
    output.write_text("export", encoding="utf-8")
    mydatabase_marker = tmp_path / "MyDatabase" / "book1.md"
    mydatabase_marker.parent.mkdir()
    mydatabase_marker.write_text("published", encoding="utf-8")
    task = store.create_task(book_id, "general", "standard")
    store.update_task(task.task_id, status="done")
    store.upsert_unit(task.task_id, "U1", "done", "cache-1")
    store.put_cache("cache-1", {"text": "cached"}, book_id=book_id)

    with TestClient(app) as client:
        preview = client.get(f"/api/books/{book_id}/deletion-preview")
        response = client.delete(f"/api/books/{book_id}")
        repeated = client.delete(f"/api/books/{book_id}")

    assert preview.status_code == 200
    assert preview.json()["task_count"] == 1
    assert "导入来源中的原始文件" in preview.json()["will_keep"]
    assert response.status_code == 200
    assert repeated.status_code == 200
    assert repeated.json()["already_absent"] is True
    assert all(not path.exists() for path in managed)
    assert external_original.read_text(encoding="utf-8") == "owner copy"
    assert output.read_text(encoding="utf-8") == "export"
    assert mydatabase_marker.read_text(encoding="utf-8") == "published"
    assert store.get_task(task.task_id) is None
    assert store.get_cache("cache-1") is None
    assert store.get_book_deletion(book_id).state == "completed"
    assert list((config.STORAGE_DIR / "recycle" / "books").glob("book1-*/manifest.json"))


def test_book_delete_is_blocked_by_active_task_or_extraction(
    tmp_path: Path, monkeypatch
) -> None:
    store = _configure(tmp_path, monkeypatch)
    active_paths = _write_book("active")
    processing_paths = _write_book("processing", status="processing")
    task = store.create_task("active", "general", "standard")

    with TestClient(app) as client:
        monkeypatch.setattr(
            books,
            "is_extraction_active",
            lambda book_id: book_id == "processing",
        )
        active = client.delete("/api/books/active")
        processing = client.delete("/api/books/processing")

    assert active.status_code == 409
    assert task.task_id in active.json()["detail"]
    assert processing.status_code == 409
    assert all(path.exists() for path in active_paths + processing_paths)


def test_book_delete_matches_exact_id_not_prefix(tmp_path: Path, monkeypatch) -> None:
    store = _configure(tmp_path, monkeypatch)
    book1_paths = _write_book("book1")
    book10_paths = _write_book("book10")

    delete_book("book1", store)

    assert all(not path.exists() for path in book1_paths)
    assert all(path.exists() for path in book10_paths)


def test_file_stage_failure_rolls_back_all_moved_files(
    tmp_path: Path, monkeypatch
) -> None:
    store = _configure(tmp_path, monkeypatch)
    paths = _write_book("book1")
    real_replace = os.replace

    def move_one_then_fail(manifest):
        first = manifest[0]
        target = Path(first["target"])
        target.parent.mkdir(parents=True, exist_ok=True)
        real_replace(first["source"], target)
        raise OSError("injected stage failure")

    monkeypatch.setattr(library_cleanup, "_stage_files", move_one_then_fail)

    with pytest.raises(OSError, match="injected stage failure"):
        delete_book("book1", store)

    assert all(path.exists() for path in paths)
    assert store.get_book_deletion("book1") is None


def test_database_finalize_failure_rolls_back_staged_files(
    tmp_path: Path, monkeypatch
) -> None:
    store = _configure(tmp_path, monkeypatch)
    paths = _write_book("book1")

    def fail_finalize(_book_id: str) -> None:
        raise OSError("injected database failure")

    monkeypatch.setattr(store, "finalize_book_deletion", fail_finalize)

    with pytest.raises(OSError, match="injected database failure"):
        delete_book("book1", store)

    assert all(path.exists() for path in paths)
    assert store.get_book_deletion("book1") is None


def test_startup_recovery_restores_incomplete_book_deletion(
    tmp_path: Path, monkeypatch
) -> None:
    store = _configure(tmp_path, monkeypatch)
    paths = _write_book("book1")
    recycle_path = library_cleanup.recycle_root() / "book1-interrupted"
    manifest = library_cleanup._manifest_for("book1", recycle_path)
    assert store.prepare_book_deletion("book1", str(recycle_path), manifest) == "preparing"
    library_cleanup._write_manifest(recycle_path, manifest)
    library_cleanup._stage_files(manifest)
    store.mark_book_deletion_staged("book1")
    assert all(not path.exists() for path in paths)

    assert recover_incomplete_book_deletions(TaskStore(config.TASK_DB)) == 1
    assert all(path.exists() for path in paths)
    assert store.get_book_deletion("book1") is None


def test_create_task_and_prepare_book_delete_are_serialized(
    tmp_path: Path,
) -> None:
    for index in range(16):
        store = TaskStore(tmp_path / f"create-delete-race-{index}.db")
        barrier = threading.Barrier(2)

        def create_task() -> str:
            barrier.wait()
            try:
                store.create_task("book1", "general", "standard")
                return "created"
            except BookDeletionInProgressError:
                return "blocked"

        def prepare_delete() -> str:
            barrier.wait()
            try:
                store.prepare_book_deletion("book1", "/tmp/recycle", [])
                return "prepared"
            except ActiveBookTasksError:
                return "blocked"

        with ThreadPoolExecutor(max_workers=2) as pool:
            created_future = pool.submit(create_task)
            prepared_future = pool.submit(prepare_delete)
            outcome = (created_future.result(), prepared_future.result())

        assert outcome in {("created", "blocked"), ("blocked", "prepared")}
        if outcome == ("created", "blocked"):
            assert len(store.list_tasks_for_book("book1")) == 1
            assert store.get_book_deletion("book1") is None
        else:
            assert store.list_tasks_for_book("book1") == []
            assert store.get_book_deletion("book1").state == "preparing"
