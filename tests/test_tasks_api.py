import time
from pathlib import Path

from fastapi.testclient import TestClient

from app import config
from app.core.extractors.base import extract_book
from app.core.task_store import TaskStore


def _prepare(tmp_path: Path, monkeypatch) -> tuple[TestClient, str, Path]:
    storage = tmp_path / "storage"
    for name in ("books", "intermediate", "output", "runs"):
        (storage / name).mkdir(parents=True)
    database = storage / "tasks.db"
    monkeypatch.setattr(config, "STORAGE_DIR", storage)
    monkeypatch.setattr(config, "BOOKS_DIR", storage / "books")
    monkeypatch.setattr(config, "INTERMEDIATE_DIR", storage / "intermediate")
    monkeypatch.setattr(config, "OUTPUT_DIR", storage / "output")
    monkeypatch.setattr(config, "RUNS_DIR", storage / "runs")
    monkeypatch.setattr(config, "TASK_DB", database, raising=False)
    monkeypatch.setattr(config, "get_api_key", lambda: "test-key")
    monkeypatch.setenv("BOOK_DISSECTOR_FAKE_DEEPSEEK", "1")
    book_id = "book123"
    source = config.BOOKS_DIR / f"{book_id}_fixture.md"
    source.write_text(
        "# 第一章\n\n准确性是效率的前提，作者提供证据和适用边界。\n",
        encoding="utf-8",
    )
    assert extract_book(book_id, source).ok
    from app.api import tasks
    from app.main import app

    tasks.reset_runtime_for_tests()
    return TestClient(app), book_id, database


def test_disassemble_api_runs_one_persistent_pipeline_task(
    tmp_path: Path, monkeypatch
) -> None:
    client, book_id, database = _prepare(tmp_path, monkeypatch)

    response = client.post(
        f"/api/books/{book_id}/disassemble",
        json={
            "book_type": "general",
            "strength": "standard",
            "cloud_consent": True,
        },
    )

    assert response.status_code == 200
    task_id = response.json()["task_id"]
    assert task_id.startswith("task_")
    terminal = None
    for _ in range(100):
        terminal = client.get(f"/api/tasks/{task_id}").json()
        if terminal["status"] in {"done", "quality_failed", "error", "cancelled"}:
            break
        time.sleep(0.02)
    assert terminal is not None
    assert terminal["status"] == "done"
    assert terminal["run_id"].startswith("run_")
    result = client.get(f"/api/tasks/{task_id}/result")
    assert result.status_code == 200
    assert result.json()["quality_report"]["status"] == "pass"
    assert result.json()["final_text"]
    assert TaskStore(database).get_task(task_id).status == "done"


def test_cloud_consent_is_required_before_queueing(tmp_path: Path, monkeypatch) -> None:
    client, book_id, database = _prepare(tmp_path, monkeypatch)

    response = client.post(
        f"/api/books/{book_id}/disassemble",
        json={"book_type": "general", "strength": "standard"},
    )

    assert response.status_code == 400
    assert "正文片段" in response.json()["detail"]
    assert TaskStore(database).list_tasks() == []


def test_estimate_endpoint_does_not_queue_and_returns_cost_range(
    tmp_path: Path, monkeypatch
) -> None:
    client, book_id, database = _prepare(tmp_path, monkeypatch)

    response = client.get(
        f"/api/books/{book_id}/estimate",
        params={"book_type": "general", "strength": "standard"},
    )

    assert response.status_code == 200
    estimate = response.json()
    assert estimate["api_calls"] >= 1
    assert estimate["cost_cny_low"] <= estimate["cost_cny_high"]
    assert estimate["pricing_source"].startswith("https://api-docs.deepseek.com/")
    assert TaskStore(database).list_tasks() == []


def test_task_list_and_cancel_endpoints_use_persistent_state(
    tmp_path: Path, monkeypatch
) -> None:
    client, book_id, database = _prepare(tmp_path, monkeypatch)
    store = TaskStore(database)
    task = store.create_task(book_id, "general", "standard")

    listed = client.get("/api/tasks").json()
    cancelled = client.post(f"/api/tasks/{task.task_id}/cancel")

    assert any(item["task_id"] == task.task_id for item in listed)
    assert cancelled.status_code == 200
    assert TaskStore(database).is_cancel_requested(task.task_id) is True
