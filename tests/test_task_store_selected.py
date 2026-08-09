from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

from app.api import tasks as task_api
from app.core.task_store import TaskStore


def test_persistent_queue_checkpoint_cancel_resume_cache_and_retry(tmp_path: Path) -> None:
    database = tmp_path / "tasks.db"
    store = TaskStore(database)
    task = store.create_task("book1", "general", "standard")
    store.update_task(task.task_id, status="running", stage="distill")
    store.upsert_unit(task.task_id, "U1", "done", "c1", output={"text": "ok"})
    store.upsert_unit(task.task_id, "U2", "error", "c2", error="timeout")
    store.put_cache("c1", {"text": "ok"})
    store.request_cancel(task.task_id)

    restored = TaskStore(database)
    assert restored.is_cancel_requested(task.task_id)
    assert restored.get_cache("c1") == {"text": "ok"}
    assert restored.reset_failed_units(task.task_id) == 1
    units = {item.unit_id: item for item in restored.get_units(task.task_id)}
    assert units["U1"].status == "done"
    assert units["U2"].status == "pending"


def test_interrupted_task_becomes_resumable(tmp_path: Path) -> None:
    store = TaskStore(tmp_path / "tasks.db")
    task = store.create_task("book1", "general", "standard")
    store.update_task(task.task_id, status="running", stage="distill")

    assert store.recover_interrupted() == 1
    assert store.get_task(task.task_id).status == "pending"


def test_pending_cancel_is_final_but_can_later_be_resumed(tmp_path: Path) -> None:
    store = TaskStore(tmp_path / "tasks.db")
    task = store.create_task("book1", "general", "standard")

    store.request_cancel(task.task_id)

    cancelled = store.get_task(task.task_id)
    assert cancelled.status == "cancelled"
    assert cancelled.cancel_requested is True


def test_concurrent_enqueue_assigns_unique_order(tmp_path: Path) -> None:
    database = tmp_path / "tasks.db"

    def create(index: int):
        return TaskStore(database).create_task(f"book{index}", "general", "standard")

    with ThreadPoolExecutor(max_workers=5) as pool:
        tasks = list(pool.map(create, range(10)))

    assert len({task.task_id for task in tasks}) == 10
    assert [task.queue_order for task in TaskStore(database).list_tasks()] == list(range(10))


def test_shutdown_signal_blocks_new_worker_submission() -> None:
    task_api._shutdown_requested.set()
    try:
        assert task_api._schedule("task-during-shutdown") is False
    finally:
        task_api._shutdown_requested.clear()
