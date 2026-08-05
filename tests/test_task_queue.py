import asyncio
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi.testclient import TestClient

from app import config
from app.api import tasks
from app.core.extractors.base import extract_book
from app.core.task_store import TaskStore
from app.main import app


def _wait_for_terminal(
    store: TaskStore, task_ids: list[str], timeout: float = 5.0
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        records = [store.get_task(task_id) for task_id in task_ids]
        if all(
            record is not None
            and record.status in {"done", "quality_failed", "error", "cancelled"}
            for record in records
        ):
            return
        time.sleep(0.01)
    states = {
        task_id: (record.status if (record := store.get_task(task_id)) else "missing")
        for task_id in task_ids
    }
    raise AssertionError(f"任务队列未在期限内结束：{states}")


def _configure_storage(tmp_path: Path, monkeypatch) -> Path:
    storage = tmp_path / "storage"
    for name in ("books", "intermediate", "output", "runs"):
        (storage / name).mkdir(parents=True)
    monkeypatch.setattr(config, "STORAGE_DIR", storage)
    monkeypatch.setattr(config, "BOOKS_DIR", storage / "books")
    monkeypatch.setattr(config, "INTERMEDIATE_DIR", storage / "intermediate")
    monkeypatch.setattr(config, "OUTPUT_DIR", storage / "output")
    monkeypatch.setattr(config, "RUNS_DIR", storage / "runs")
    monkeypatch.setattr(config, "TASK_DB", storage / "tasks.db", raising=False)
    return storage


def test_pending_cancel_becomes_terminal_instead_of_stalling(tmp_path: Path) -> None:
    store = TaskStore(tmp_path / "tasks.db")
    task = store.create_task("book-cancel", "general", "standard")

    store.request_cancel(task.task_id)

    cancelled = store.get_task(task.task_id)
    assert cancelled is not None
    assert cancelled.status == "cancelled"
    assert cancelled.stage == "cancelled"
    assert cancelled.cancel_requested is True


def test_recovery_finishes_a_running_task_with_a_persisted_cancel_request(
    tmp_path: Path,
) -> None:
    store = TaskStore(tmp_path / "tasks.db")
    task = store.create_task("book-restart-cancel", "general", "standard")
    assert store.claim_task(task.task_id) is True
    store.request_cancel(task.task_id)

    recovered = TaskStore(store.database)
    assert recovered.recover_interrupted() == 1

    cancelled = recovered.get_task(task.task_id)
    assert cancelled is not None
    assert cancelled.status == "cancelled"
    assert cancelled.stage == "cancelled"
    assert cancelled.cancel_requested is True


def test_concurrent_enqueues_get_unique_monotonic_positions(tmp_path: Path) -> None:
    store = TaskStore(tmp_path / "tasks.db")
    task_count = 32
    start = threading.Barrier(task_count)

    def enqueue(index: int):
        start.wait()
        return store.create_task(f"book-{index}", "general", "standard")

    with ThreadPoolExecutor(max_workers=task_count) as executor:
        queued = list(executor.map(enqueue, range(task_count)))

    assert sorted(task.queue_order for task in queued) == list(range(task_count))
    assert len(store.list_tasks()) == task_count


def test_move_before_self_is_an_idempotent_noop(tmp_path: Path) -> None:
    store = TaskStore(tmp_path / "tasks.db")
    task = store.create_task("book-move", "general", "standard")

    store.move_before(task.task_id, task.task_id)

    assert [item.task_id for item in store.list_tasks()] == [task.task_id]


def test_queue_api_returns_terminal_cancel_and_accepts_self_move(
    tmp_path: Path, monkeypatch
) -> None:
    _configure_storage(tmp_path, monkeypatch)
    tasks.reset_runtime_for_tests()
    store = tasks._get_store()
    cancelled_task = store.create_task("book-api-cancel", "general", "standard")
    moved_task = store.create_task("book-api-move", "general", "standard")
    client = TestClient(app)
    try:
        cancel_response = client.post(f"/api/tasks/{cancelled_task.task_id}/cancel")
        move_response = client.post(
            f"/api/tasks/{moved_task.task_id}/move",
            json={"before_task_id": moved_task.task_id},
        )
        assert cancel_response.status_code == 200
        assert cancel_response.json()["status"] == "cancelled"
        assert move_response.status_code == 200
    finally:
        tasks.reset_runtime_for_tests()


def test_worker_marks_pre_pipeline_failure_terminal_without_resubmitting(
    tmp_path: Path, monkeypatch
) -> None:
    database = tmp_path / "tasks.db"
    monkeypatch.setattr(config, "TASK_DB", database)
    tasks.reset_runtime_for_tests()
    store = tasks._get_store()
    task = store.create_task("book-broken", "general", "standard")
    calls = 0

    async def fail_before_pipeline_handler(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("fixture failed before pipeline error handling")
        kwargs["task_store"].update_task(kwargs["task_id"], status="done")

    monkeypatch.setattr(tasks, "run_pipeline", fail_before_pipeline_handler)
    try:
        tasks._schedule_pending()
        _wait_for_terminal(store, [task.task_id])
        failed = store.get_task(task.task_id)
        assert failed is not None
        assert failed.status == "error"
        assert "fixture failed" in failed.error
        assert calls == 1
    finally:
        tasks.reset_runtime_for_tests()


def test_ten_task_queue_limits_concurrency_and_isolates_one_failure(
    tmp_path: Path, monkeypatch
) -> None:
    database = tmp_path / "tasks.db"
    monkeypatch.setattr(config, "TASK_DB", database)
    tasks.reset_runtime_for_tests()
    store = tasks._get_store()
    queued = [
        store.create_task(f"book-{index}", "general", "standard")
        for index in range(10)
    ]
    store.move_before(queued[-1].task_id, queued[0].task_id)
    expected_first = {queued[-1].task_id, queued[0].task_id}
    failed_task_id = queued[4].task_id
    state_lock = threading.Lock()
    started: list[str] = []
    active = 0
    max_active = 0

    async def fake_pipeline(*args, **kwargs):
        nonlocal active, max_active
        task_store = kwargs["task_store"]
        task_id = kwargs["task_id"]
        task_store.update_task(task_id, status="running", stage="distill")
        with state_lock:
            started.append(task_id)
            active += 1
            max_active = max(max_active, active)
        try:
            await asyncio.sleep(0.02)
            if task_id == failed_task_id:
                task_store.update_task(
                    task_id,
                    status="error",
                    stage="error",
                    error="isolated fixture failure",
                )
                raise RuntimeError("isolated fixture failure")
            task_store.update_task(task_id, status="done", stage="export")
        finally:
            with state_lock:
                active -= 1

    monkeypatch.setattr(tasks, "run_pipeline", fake_pipeline)
    try:
        tasks._schedule_pending()
        _wait_for_terminal(store, [task.task_id for task in queued])
        statuses = {}
        for task in queued:
            record = store.get_task(task.task_id)
            assert record is not None
            statuses[task.task_id] = record.status
        assert set(started[:2]) == expected_first
        assert len(started) == 10
        assert max_active == tasks.MAX_ACTIVE_BOOK_TASKS
        assert statuses[failed_task_id] == "error"
        assert list(statuses.values()).count("done") == 9
    finally:
        tasks.reset_runtime_for_tests()


def test_ten_book_queue_completes_with_the_real_pipeline_and_fake_model(
    tmp_path: Path, monkeypatch
) -> None:
    _configure_storage(tmp_path, monkeypatch)
    monkeypatch.setenv("BOOK_DISSECTOR_FAKE_DEEPSEEK", "1")
    tasks.reset_runtime_for_tests()
    store = tasks._get_store()
    queued = []
    for index in range(10):
        book_id = f"book-e2e-{index}"
        source = config.BOOKS_DIR / f"{book_id}_fixture.md"
        source.write_text(
            f"# 第 {index + 1} 本\n\n"
            "准确性是效率的前提，证据需要保留适用边界。\n",
            encoding="utf-8",
        )
        assert extract_book(book_id, source).ok
        queued.append(store.create_task(book_id, "general", "standard"))

    try:
        tasks._schedule_pending()
        _wait_for_terminal(store, [task.task_id for task in queued], timeout=10.0)
        completed = [store.get_task(task.task_id) for task in queued]
        assert all(task is not None and task.status == "done" for task in completed)
        assert all(
            task is not None and task.run_id.startswith("run_") for task in completed
        )
        assert len(list(config.OUTPUT_DIR.glob("*.md"))) == 10
    finally:
        tasks.reset_runtime_for_tests()
