import json
import threading
from pathlib import Path

from fastapi.testclient import TestClient

from app import config
from app.api import books, tasks
from app.main import app


def _configure_storage(tmp_path: Path, monkeypatch) -> None:
    storage = tmp_path / "storage"
    monkeypatch.setattr(config, "STORAGE_DIR", storage)
    monkeypatch.setattr(config, "BOOKS_DIR", storage / "books")
    monkeypatch.setattr(config, "INTERMEDIATE_DIR", storage / "intermediate")
    monkeypatch.setattr(config, "OUTPUT_DIR", storage / "output")
    monkeypatch.setattr(config, "TASK_DB", storage / "tasks.db")
    monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "config.json")
    config.ensure_dirs()
    tasks.reset_runtime_for_tests()


def _write_finished_extraction(book_id: str) -> None:
    (config.BOOKS_DIR / f"{book_id}.txt").write_text("小说正文", encoding="utf-8")
    (config.BOOKS_DIR / f"{book_id}.meta.json").write_text(
        json.dumps(
            {
                "book_id": book_id,
                "title": "慢提取小说",
                "author": "",
                "source_format": "txt",
                "word_count": 4,
                "chapters": [],
                "extract_status": "ok",
                "extract_error": "",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def test_slow_upload_persists_processing_until_extraction_finishes(
    tmp_path: Path, monkeypatch
) -> None:
    _configure_storage(tmp_path, monkeypatch)
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def slow_extract(book_id: str, _source_path: Path) -> None:
        started.set()
        release.wait(timeout=5)
        _write_finished_extraction(book_id)
        finished.set()

    monkeypatch.setattr(books, "extract_book", slow_extract)

    try:
        with TestClient(app) as client:
            response = client.post(
                "/api/books/upload",
                files={"file": ("novel.txt", b"fixture body", "text/plain")},
            )
            assert response.status_code == 200
            book_id = response.json()["id"]
            assert started.wait(timeout=2)

            meta = json.loads(
                (config.BOOKS_DIR / f"{book_id}.meta.json").read_text(encoding="utf-8")
            )
            assert meta["extract_status"] == "processing"
            assert client.get("/api/books").json()[0]["extract_status"] == "processing"

            release.set()
            assert finished.wait(timeout=2)
            assert client.get("/api/books").json()[0]["extract_status"] == "ok"
    finally:
        release.set()
        finished.wait(timeout=2)
        tasks.reset_runtime_for_tests()


def test_service_restart_recovers_processing_extraction(tmp_path: Path, monkeypatch) -> None:
    _configure_storage(tmp_path, monkeypatch)
    book_id = "book-recovery"
    source = config.BOOKS_DIR / f"{book_id}_novel.txt"
    source.write_text("fixture body", encoding="utf-8")
    (config.BOOKS_DIR / f"{book_id}.meta.json").write_text(
        json.dumps(
            {
                "book_id": book_id,
                "source_format": "txt",
                "extract_status": "processing",
                "extract_error": "",
            }
        ),
        encoding="utf-8",
    )
    finished = threading.Event()

    def recovered_extract(recovered_id: str, recovered_source: Path) -> None:
        assert recovered_id == book_id
        assert recovered_source == source
        _write_finished_extraction(recovered_id)
        finished.set()

    monkeypatch.setattr(books, "extract_book", recovered_extract)

    assert books.recover_incomplete_extractions() == 1
    assert finished.wait(timeout=2)
    assert books._load_meta(book_id)["extract_status"] == "ok"
    assert books.recover_incomplete_extractions() == 0


def test_frontend_repolls_books_and_recovers_task_polling() -> None:
    source = (config.PROJECT_ROOT / "app" / "static" / "app.js").read_text(
        encoding="utf-8"
    )

    assert "function scheduleBookRefresh" in source
    assert '["pending", "processing"].includes(book.extract_status)' in source
    assert "scheduleBookRefresh" in source[source.index("async function refreshBooks") :]
    assert "连接中断，正在重试" in source[source.index("async function pollCurrentTask") :]


def test_opening_another_book_reenables_queue_submission() -> None:
    source = (config.PROJECT_ROOT / "app" / "static" / "app.js").read_text(
        encoding="utf-8"
    )
    open_distill = source[
        source.index("async function openDistill") :
        source.index('$("#distill-type").addEventListener')
    ]

    assert '$("#btn-start-distill").disabled = false;' in open_distill
