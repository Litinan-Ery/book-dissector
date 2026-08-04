"""运行创建、源文件指纹与稳定来源 ID。"""
from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime
from pathlib import Path

from .. import config
from ..models.domain import RunManifest
from .artifacts import ArtifactStore


def fingerprint_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def make_source_id(source_fingerprint: str, start_char: int, end_char: int) -> str:
    if start_char < 0 or end_char <= start_char:
        raise ValueError("source range must be non-empty")
    material = f"{source_fingerprint}:{start_char}:{end_char}".encode("utf-8")
    return "S" + hashlib.sha256(material).hexdigest()[:16].upper()


def create_run(
    *,
    book_id: str,
    source_path: Path,
    store: ArtifactStore | None = None,
    book_type: str = "general",
    strength: str = "standard",
) -> RunManifest:
    if not source_path.is_file():
        raise FileNotFoundError(source_path)
    created_at = datetime.now(UTC)
    run_id = f"run_{created_at:%Y%m%dT%H%M%S}_{uuid.uuid4().hex[:8]}"
    manifest = RunManifest(
        run_id=run_id,
        book_id=book_id,
        source_fingerprint=fingerprint_file(source_path),
        created_at=created_at,
        book_type=book_type,
        strength=strength,
        model=config.DEEPSEEK_MODEL,
    )
    (store or ArtifactStore(config.RUNS_DIR)).create_run(manifest)
    return manifest

