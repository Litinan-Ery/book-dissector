"""版本化运行产物的原子读写。"""
from __future__ import annotations

import json
import os
import uuid
from pathlib import Path, PurePosixPath
from typing import Any, TypeVar

from pydantic import BaseModel

from ..models.domain import RunManifest
from .atomic import atomic_write_text

ModelT = TypeVar("ModelT", bound=BaseModel)


class ArtifactStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    @staticmethod
    def _validate_run_id(run_id: str) -> None:
        if not run_id or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for char in run_id):
            raise ValueError("invalid run_id")

    @staticmethod
    def _validate_relative_path(relative_path: str) -> PurePosixPath:
        candidate = PurePosixPath(relative_path)
        if candidate.is_absolute() or not candidate.parts or ".." in candidate.parts:
            raise ValueError("artifact path must stay inside the run directory")
        return candidate

    def run_dir(self, run_id: str) -> Path:
        self._validate_run_id(run_id)
        return self.root / run_id

    def create_run(self, manifest: RunManifest) -> Path:
        run_dir = self.run_dir(manifest.run_id)
        run_dir.mkdir(parents=True, exist_ok=False)
        self._atomic_write_json(run_dir / "manifest.json", manifest)
        return run_dir

    def write_json(self, run_id: str, relative_path: str, value: Any) -> Path:
        run_dir = self.run_dir(run_id)
        if not run_dir.is_dir():
            raise FileNotFoundError(f"run does not exist: {run_id}")
        relative = self._validate_relative_path(relative_path)
        destination = run_dir.joinpath(*relative.parts)
        destination.parent.mkdir(parents=True, exist_ok=True)
        self._atomic_write_json(destination, value)
        return destination

    def read_json(self, run_id: str, relative_path: str) -> Any:
        relative = self._validate_relative_path(relative_path)
        path = self.run_dir(run_id).joinpath(*relative.parts)
        return json.loads(path.read_text(encoding="utf-8"))

    def write_text(self, run_id: str, relative_path: str, content: str) -> Path:
        run_dir = self.run_dir(run_id)
        if not run_dir.is_dir():
            raise FileNotFoundError(f"run does not exist: {run_id}")
        relative = self._validate_relative_path(relative_path)
        destination = run_dir.joinpath(*relative.parts)
        atomic_write_text(destination, content)
        return destination

    def read_model(
        self, run_id: str, relative_path: str, model_type: type[ModelT]
    ) -> ModelT:
        return model_type.model_validate(self.read_json(run_id, relative_path))

    @staticmethod
    def _atomic_write_json(path: Path, value: Any) -> None:
        payload = value.model_dump(mode="json") if isinstance(value, BaseModel) else value
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, path)
        finally:
            if temporary.exists():
                temporary.unlink()
