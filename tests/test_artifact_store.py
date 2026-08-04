import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.core.artifacts import ArtifactStore
from app.core.runs import create_run, make_source_id
from app.models.domain import RunManifest


def _manifest(run_id: str) -> RunManifest:
    return RunManifest(
        run_id=run_id,
        book_id="book123",
        source_fingerprint="b" * 64,
        created_at=datetime(2026, 8, 5, tzinfo=UTC),
    )


def test_create_run_writes_versioned_manifest(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path)

    run_dir = store.create_run(_manifest("run_20260805_abcdef12"))

    assert run_dir == tmp_path / "run_20260805_abcdef12"
    data = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert data["schema_version"] == "1.0"
    assert data["book_id"] == "book123"


def test_write_json_is_readable_and_leaves_no_temp_file(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path)
    store.create_run(_manifest("run_20260805_abcdef12"))

    path = store.write_json("run_20260805_abcdef12", "quality/report.json", {"status": "pass"})

    assert store.read_json("run_20260805_abcdef12", "quality/report.json") == {"status": "pass"}
    assert path == tmp_path / "run_20260805_abcdef12" / "quality" / "report.json"
    assert not list(tmp_path.rglob("*.tmp"))


def test_store_rejects_path_traversal(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path)
    store.create_run(_manifest("run_20260805_abcdef12"))

    with pytest.raises(ValueError):
        store.write_json("run_20260805_abcdef12", "../outside.json", {})
    with pytest.raises(ValueError):
        store.write_json("../outside", "report.json", {})


def test_create_run_never_overwrites_existing_manifest(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path)
    manifest = _manifest("run_20260805_abcdef12")
    store.create_run(manifest)

    with pytest.raises(FileExistsError):
        store.create_run(manifest)


def test_create_run_fingerprints_source_and_persists_manifest(tmp_path: Path) -> None:
    source = tmp_path / "source.md"
    source.write_text("# 第一章\n正文", encoding="utf-8")
    store = ArtifactStore(tmp_path / "runs")

    manifest = create_run(
        book_id="book123",
        source_path=source,
        store=store,
        book_type="technical",
        strength="conservative",
    )

    persisted = store.read_model(manifest.run_id, "manifest.json", RunManifest)
    assert persisted.source_fingerprint == manifest.source_fingerprint
    assert persisted.book_type == "technical"
    assert persisted.strength == "conservative"
    assert len(persisted.source_fingerprint) == 64


def test_source_ids_are_stable_for_same_source_range() -> None:
    fingerprint = "c" * 64

    first = make_source_id(fingerprint, 10, 20)
    second = make_source_id(fingerprint, 10, 20)
    different = make_source_id(fingerprint, 11, 20)

    assert first == second
    assert first.startswith("S")
    assert first != different
