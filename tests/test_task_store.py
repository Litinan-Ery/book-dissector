from pathlib import Path

from app.core.task_store import TaskStore


def test_task_state_survives_new_store_instance(tmp_path: Path) -> None:
    database = tmp_path / "tasks.db"
    first = TaskStore(database)
    task = first.create_task(
        book_id="book123", book_type="general", strength="standard"
    )
    first.update_task(
        task.task_id,
        status="running",
        stage="distill",
        current=2,
        total=5,
        message="提炼第 2/5 单元",
    )

    restored = TaskStore(database).get_task(task.task_id)

    assert restored is not None
    assert restored.status == "running"
    assert restored.stage == "distill"
    assert restored.current == 2
    assert restored.total == 5


def test_recovery_marks_interrupted_work_resumable(tmp_path: Path) -> None:
    store = TaskStore(tmp_path / "tasks.db")
    task = store.create_task(
        book_id="book123", book_type="general", strength="standard"
    )
    store.update_task(task.task_id, status="running", stage="distill")

    recovered = store.recover_interrupted()
    task_after = store.get_task(task.task_id)

    assert recovered == 1
    assert task_after is not None
    assert task_after.status == "pending"
    assert task_after.stage == "resume"


def test_cancel_request_is_persistent(tmp_path: Path) -> None:
    database = tmp_path / "tasks.db"
    store = TaskStore(database)
    task = store.create_task(
        book_id="book123", book_type="general", strength="standard"
    )

    store.request_cancel(task.task_id)

    assert store.is_cancel_requested(task.task_id) is True
    assert TaskStore(database).is_cancel_requested(task.task_id) is True


def test_unit_checkpoints_and_cache_round_trip(tmp_path: Path) -> None:
    store = TaskStore(tmp_path / "tasks.db")
    task = store.create_task(
        book_id="book123", book_type="general", strength="standard"
    )
    output = {"knowledge_units": [{"knowledge_id": "K1"}]}

    store.upsert_unit(
        task.task_id,
        unit_id="U1",
        status="done",
        cache_key="cache-1",
        attempts=1,
        output=output,
    )
    store.put_cache("cache-1", output)

    unit = store.get_units(task.task_id)[0]
    assert unit.status == "done"
    assert unit.output == output
    assert store.get_cache("cache-1") == output


def test_retry_resets_only_failed_units(tmp_path: Path) -> None:
    store = TaskStore(tmp_path / "tasks.db")
    task = store.create_task(
        book_id="book123", book_type="general", strength="standard"
    )
    store.upsert_unit(task.task_id, "U1", "done", "c1", attempts=1, output={"ok": 1})
    store.upsert_unit(task.task_id, "U2", "error", "c2", attempts=2, error="timeout")

    reset = store.reset_failed_units(task.task_id)
    units = {unit.unit_id: unit for unit in store.get_units(task.task_id)}

    assert reset == 1
    assert units["U1"].status == "done"
    assert units["U1"].output == {"ok": 1}
    assert units["U2"].status == "pending"
    assert units["U2"].error == ""


def test_queue_order_can_be_changed_without_losing_tasks(tmp_path: Path) -> None:
    store = TaskStore(tmp_path / "tasks.db")
    first = store.create_task("book1", "general", "standard")
    second = store.create_task("book2", "general", "standard")
    third = store.create_task("book3", "general", "standard")

    store.move_before(third.task_id, first.task_id)

    assert [task.task_id for task in store.list_tasks()] == [
        third.task_id,
        first.task_id,
        second.task_id,
    ]

