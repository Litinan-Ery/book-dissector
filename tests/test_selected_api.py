import json
from pathlib import Path

from fastapi.testclient import TestClient

from app import config
from app.api import tasks
from app.main import app


def _configure(tmp_path: Path, monkeypatch) -> str:
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
    monkeypatch.setattr(config, "TASK_DB", storage / "tasks.db")
    monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr(tasks, "_schedule_pending", lambda: None)
    tasks.reset_runtime_for_tests()
    book_id = "book1"
    (books / f"{book_id}.txt").write_text("正文" * 100, encoding="utf-8")
    (books / f"{book_id}.meta.json").write_text(
        json.dumps({"extract_status": "ok"}), encoding="utf-8"
    )
    config.set_api_key("test-key")
    return book_id


def test_estimate_and_first_cloud_consent(tmp_path: Path, monkeypatch) -> None:
    book_id = _configure(tmp_path, monkeypatch)
    with TestClient(app) as client:
        estimate = client.get(f"/api/books/{book_id}/estimate")
        assert estimate.status_code == 200
        assert {"input_tokens", "output_tokens", "api_calls", "time_seconds_low", "cost_cny_low"} <= set(estimate.json())

        denied = client.post(
            f"/api/books/{book_id}/disassemble",
            json={"book_type": "general", "strength": "standard", "cloud_consent": False},
        )
        assert denied.status_code == 400

        accepted = client.post(
            f"/api/books/{book_id}/disassemble",
            json={"book_type": "general", "strength": "standard", "cloud_consent": True},
        )
        assert accepted.status_code == 200
        assert config.has_cloud_consent() is True


def test_estimate_uses_same_top_level_chapter_plan_as_distiller(
    tmp_path: Path, monkeypatch
) -> None:
    book_id = _configure(tmp_path, monkeypatch)
    text = "# 第一章\n导言\n## 第一节\n细节\n# 第二章\n结论"
    second_section = text.index("## 第一节")
    second_chapter = text.index("# 第二章")
    (config.BOOKS_DIR / f"{book_id}.txt").write_text(text, encoding="utf-8")
    (config.BOOKS_DIR / f"{book_id}.meta.json").write_text(
        json.dumps(
            {
                "extract_status": "ok",
                "chapters": [
                    {"title": "第一章", "level": 1, "start_char": 0, "end_char": second_section},
                    {"title": "第一节", "level": 2, "start_char": second_section, "end_char": second_chapter},
                    {"title": "第二章", "level": 1, "start_char": second_chapter, "end_char": len(text)},
                ],
            }
        ),
        encoding="utf-8",
    )

    with TestClient(app) as client:
        response = client.get(f"/api/books/{book_id}/estimate")

    assert response.status_code == 200
    assert response.json()["api_calls"] == 2
