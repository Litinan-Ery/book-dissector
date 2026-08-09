from __future__ import annotations

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import config
from app.api import tasks
from app.core.task_store import TaskStore
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


@pytest.mark.parametrize("status", ["pending", "done", "error", "cancelled"])
def test_non_running_task_delete_is_immediate_and_preserves_cache(
    tmp_path: Path, status: str
) -> None:
    store = TaskStore(tmp_path / f"{status}.db")
    task = store.create_task("book1", "general", "standard")
    store.update_task(task.task_id, status=status)
    store.upsert_unit(task.task_id, "U1", "done", "cache-1", output={"text": "ok"})
    store.put_cache("cache-1", {"text": "reusable"}, book_id="book1")

    assert store.request_task_delete(task.task_id) == "deleted"
    assert store.get_task(task.task_id) is None
    assert store.get_units(task.task_id) == []
    assert store.get_cache("cache-1") == {"text": "reusable"}
    assert store.request_task_delete(task.task_id) == "absent"


def test_running_task_delete_waits_for_boundary_then_finalizes(tmp_path: Path) -> None:
    store = TaskStore(tmp_path / "tasks.db")
    task = store.create_task("book1", "general", "standard")
    store.update_task(task.task_id, status="running", stage="distill")

    assert store.request_task_delete(task.task_id) == "deleting"
    deleting = store.get_task(task.task_id)
    assert deleting is not None
    assert deleting.delete_requested is True
    assert deleting.cancel_requested is True
    assert deleting.stage == "deleting"
    assert store.claim_task(task.task_id) is False

    assert store.finalize_task_delete(task.task_id) is True
    assert store.get_task(task.task_id) is None


def test_concurrent_task_delete_is_idempotent(tmp_path: Path) -> None:
    database = tmp_path / "tasks.db"
    task = TaskStore(database).create_task("book1", "general", "standard")

    def remove(_: int) -> str:
        return TaskStore(database).request_task_delete(task.task_id)

    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(remove, range(32)))

    assert outcomes.count("deleted") == 1
    assert set(outcomes) <= {"deleted", "absent"}
    assert TaskStore(database).get_task(task.task_id) is None


def test_claim_and_delete_race_has_only_two_consistent_outcomes(
    tmp_path: Path,
) -> None:
    for index in range(24):
        store = TaskStore(tmp_path / f"race-{index}.db")
        task = store.create_task("book1", "general", "standard")
        barrier = threading.Barrier(2)

        def claim() -> bool:
            barrier.wait()
            return store.claim_task(task.task_id)

        def remove() -> str:
            barrier.wait()
            return store.request_task_delete(task.task_id)

        with ThreadPoolExecutor(max_workers=2) as pool:
            claimed_future = pool.submit(claim)
            removed_future = pool.submit(remove)
            claimed = claimed_future.result()
            removed = removed_future.result()

        if claimed:
            assert removed == "deleting"
            current = store.get_task(task.task_id)
            assert current is not None and current.delete_requested
            assert store.finalize_task_delete(task.task_id)
        else:
            assert removed == "deleted"
        assert store.get_task(task.task_id) is None


def test_task_delete_api_returns_202_for_running_and_is_idempotent(
    tmp_path: Path, monkeypatch
) -> None:
    store = _configure(tmp_path, monkeypatch)
    pending = store.create_task("book1", "general", "standard")
    running = store.create_task("book2", "general", "standard")

    with TestClient(app) as client:
        # Lifespan recovery intentionally converts a previous-process running task
        # back to pending, so mark it running after startup to model a live worker.
        store.update_task(running.task_id, status="running")
        first = client.delete(f"/api/tasks/{pending.task_id}")
        repeated = client.delete(f"/api/tasks/{pending.task_id}")
        delayed = client.delete(f"/api/tasks/{running.task_id}")

    assert first.status_code == 200
    assert first.json()["state"] == "deleted"
    assert repeated.status_code == 200
    assert repeated.json()["already_absent"] is True
    assert delayed.status_code == 202
    assert delayed.json()["state"] == "deleting"
    assert store.get_task(running.task_id).delete_requested is True


def test_restart_removes_delete_requested_running_task(tmp_path: Path) -> None:
    store = TaskStore(tmp_path / "tasks.db")
    task = store.create_task("book1", "general", "standard")
    store.update_task(task.task_id, status="running")
    assert store.request_task_delete(task.task_id) == "deleting"

    assert TaskStore(tmp_path / "tasks.db").recover_interrupted() == 1
    assert store.get_task(task.task_id) is None


def test_schema_migration_adds_v021_columns_to_existing_database(tmp_path: Path) -> None:
    database = tmp_path / "legacy.db"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE tasks (
                task_id TEXT PRIMARY KEY, book_id TEXT NOT NULL,
                book_type TEXT NOT NULL, strength TEXT NOT NULL,
                status TEXT NOT NULL, stage TEXT NOT NULL,
                current INTEGER NOT NULL DEFAULT 0, total INTEGER NOT NULL DEFAULT 0,
                error TEXT NOT NULL DEFAULT '', message TEXT NOT NULL DEFAULT '',
                cancel_requested INTEGER NOT NULL DEFAULT 0,
                queue_order INTEGER NOT NULL,
                result_json TEXT NOT NULL DEFAULT '{}',
                estimate_json TEXT NOT NULL DEFAULT '{}',
                metrics_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE unit_cache (
                cache_key TEXT PRIMARY KEY, output_json TEXT NOT NULL,
                created_at TEXT NOT NULL, last_hit_at TEXT NOT NULL,
                hit_count INTEGER NOT NULL DEFAULT 0
            );
            """
        )

    TaskStore(database)
    with sqlite3.connect(database) as connection:
        task_columns = {row[1] for row in connection.execute("PRAGMA table_info(tasks)")}
        cache_columns = {row[1] for row in connection.execute("PRAGMA table_info(unit_cache)")}

    assert "delete_requested" in task_columns
    assert "book_id" in cache_columns


def test_reveal_output_uses_safe_finder_argv_and_does_not_mutate_file(
    tmp_path: Path, monkeypatch
) -> None:
    store = _configure(tmp_path, monkeypatch)
    output = config.OUTPUT_DIR / "a name;$(touch nope).md"
    output.write_text("immutable output", encoding="utf-8")
    before = output.read_bytes()
    task = store.create_task("book1", "general", "standard")
    store.update_task(task.task_id, status="done", result={"output_path": str(output)})
    calls: list[tuple[list[str], dict]] = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))

    monkeypatch.setattr(tasks.sys, "platform", "darwin")
    monkeypatch.setattr(tasks.subprocess, "run", fake_run)

    with TestClient(app) as client:
        response = client.post(f"/api/tasks/{task.task_id}/reveal-output")

    assert response.status_code == 200
    assert calls == [
        (
            ["open", "-R", str(output.resolve())],
            {
                "check": True,
                "capture_output": True,
                "text": True,
                "timeout": 5,
                "shell": False,
            },
        )
    ]
    assert output.read_bytes() == before
    assert not (tmp_path / "nope").exists()


def test_reveal_output_rejects_unfinished_or_missing_output(
    tmp_path: Path, monkeypatch
) -> None:
    store = _configure(tmp_path, monkeypatch)
    pending = store.create_task("book1", "general", "standard")
    missing = store.create_task("book2", "general", "standard")
    store.update_task(
        missing.task_id,
        status="done",
        result={"output_path": str(tmp_path / "missing.md")},
    )

    with TestClient(app) as client:
        unfinished_response = client.post(f"/api/tasks/{pending.task_id}/reveal-output")
        missing_response = client.post(f"/api/tasks/{missing.task_id}/reveal-output")

    assert unfinished_response.status_code == 409
    assert missing_response.status_code == 410


def test_reveal_output_reports_platform_and_finder_failures(
    tmp_path: Path, monkeypatch
) -> None:
    store = _configure(tmp_path, monkeypatch)
    output = config.OUTPUT_DIR / "result.md"
    output.write_text("output", encoding="utf-8")
    task = store.create_task("book1", "general", "standard")
    store.update_task(task.task_id, status="done", result={"output_path": str(output)})

    monkeypatch.setattr(tasks.sys, "platform", "linux")
    with TestClient(app) as client:
        unsupported = client.post(f"/api/tasks/{task.task_id}/reveal-output")
    assert unsupported.status_code == 501

    def fail_finder(*_args, **_kwargs):
        raise tasks.subprocess.SubprocessError("injected Finder failure")

    monkeypatch.setattr(tasks.sys, "platform", "darwin")
    monkeypatch.setattr(tasks.subprocess, "run", fail_finder)
    with TestClient(app) as client:
        failed = client.post(f"/api/tasks/{task.task_id}/reveal-output")
    assert failed.status_code == 502
    assert "Finder 打开失败" in failed.json()["detail"]


def test_management_endpoints_reject_illegal_task_id(
    tmp_path: Path, monkeypatch
) -> None:
    _configure(tmp_path, monkeypatch)

    with TestClient(app) as client:
        delete_response = client.delete("/api/tasks/not-a-task")
        reveal_response = client.post("/api/tasks/not-a-task/reveal-output")

    assert delete_response.status_code == 400
    assert reveal_response.status_code == 400


def test_management_controls_are_present_and_done_only_reveal_is_guarded() -> None:
    source = (config.PROJECT_ROOT / "app" / "static" / "app.js").read_text(
        encoding="utf-8"
    )
    html = (config.PROJECT_ROOT / "app" / "static" / "index.html").read_text(
        encoding="utf-8"
    )

    assert 'el("button", "删除书籍"' in source
    assert 'el("button", deleting ? "正在删除…" : "删除任务"' in source
    assert 'task.status === "done"' in source
    assert 'el("button", "打开文件夹"' in source
    assert 'method: "DELETE"' in source
    assert 'id="confirm-dialog"' in html
