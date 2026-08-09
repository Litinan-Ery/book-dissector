import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from app import config
from app.core import exporter
from app.core.hooks import PostExportHookError, run_post_export_hooks


def test_disabled_hook_does_not_start_a_subprocess(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "summary.md"
    output.write_text("# summary\n", encoding="utf-8")
    monkeypatch.setattr(config, "load_config", lambda: {})

    def unexpected(*args, **kwargs):
        raise AssertionError("subprocess must not run while hook is disabled")

    monkeypatch.setattr(subprocess, "run", unexpected)
    result = run_post_export_hooks(output, book_id="book-1", book_meta={})

    assert result.enabled is False


def test_enabled_hook_calls_mydatabase_with_a_stable_source_fingerprint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "MyDatabase"
    (project / "src").mkdir(parents=True)
    database = project / "data" / "mydatabase.sqlite3"
    database.parent.mkdir()
    database.touch()
    vault = project / "vault"
    books = tmp_path / "books"
    books.mkdir()
    source = books / "book-1_fixture.epub"
    source.write_bytes(b"stable source bytes")
    output = tmp_path / "summary.md"
    output.write_text("# summary\n", encoding="utf-8")
    monkeypatch.setattr(config, "BOOKS_DIR", books)
    monkeypatch.setattr(
        config,
        "load_config",
        lambda: {
            "mydatabase_hook": {
                "enabled": True,
                "project_root": str(project),
                "database_path": "data/mydatabase.sqlite3",
                "vault_path": "vault",
            }
        },
    )
    captured = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps(
                {
                    "entry_id": "entry-1",
                    "entry_action": "matched_title",
                    "note_action": "created",
                    "note_path": "vault/Notes/Book Summaries/note.md",
                }
            ),
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = run_post_export_hooks(
        output,
        book_id="book-1",
        book_meta={"title": "局外人", "author": "加缪"},
    )

    command = captured["command"]
    fingerprint = hashlib.sha256(source.read_bytes()).hexdigest()
    assert command[command.index("--source-fingerprint") + 1] == fingerprint
    assert command[command.index("--title") + 1] == "局外人"
    assert command[command.index("--author") + 1] == "加缪"
    assert captured["kwargs"]["cwd"] == project.resolve()
    assert result.entry_id == "entry-1"
    assert result.note_action == "created"


def test_enabled_hook_surfaces_import_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "MyDatabase"
    (project / "src").mkdir(parents=True)
    database = project / "data" / "mydatabase.sqlite3"
    database.parent.mkdir()
    database.touch()
    books = tmp_path / "books"
    books.mkdir()
    (books / "book-1.txt").write_text("source", encoding="utf-8")
    output = tmp_path / "summary.md"
    output.write_text("summary", encoding="utf-8")
    monkeypatch.setattr(config, "BOOKS_DIR", books)
    monkeypatch.setattr(
        config,
        "load_config",
        lambda: {
            "mydatabase_hook": {
                "enabled": True,
                "project_root": str(project),
            }
        },
    )
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(
            command, 1, stdout="", stderr="ambiguous title"
        ),
    )

    with pytest.raises(PostExportHookError, match="ambiguous title"):
        run_post_export_hooks(output, book_id="book-1", book_meta={"title": "同名书"})


def test_formal_export_runs_hook_after_writing_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    books = tmp_path / "books"
    intermediate = tmp_path / "intermediate"
    output = tmp_path / "output"
    for directory in (books, intermediate, output):
        directory.mkdir()
    book_id = "book-1"
    meta = {"title": "测试书", "author": "作者", "source_format": "md"}
    (books / f"{book_id}.meta.json").write_text(
        json.dumps(meta, ensure_ascii=False), encoding="utf-8"
    )
    (intermediate / f"{book_id}.distilled.md").write_text(
        "# 精华\n", encoding="utf-8"
    )
    (intermediate / f"{book_id}.distill.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(config, "BOOKS_DIR", books)
    monkeypatch.setattr(config, "INTERMEDIATE_DIR", intermediate)
    monkeypatch.setattr(config, "OUTPUT_DIR", output)
    monkeypatch.setattr(config, "ensure_dirs", lambda: None)
    calls = []

    def fake_hooks(path, *, book_id, book_meta):
        assert path.is_file()
        calls.append((path, book_id, book_meta))

    monkeypatch.setattr(exporter, "run_post_export_hooks", fake_hooks)
    destination = exporter.export_book(book_id)

    assert destination.is_file()
    assert calls == [(destination, book_id, meta)]
