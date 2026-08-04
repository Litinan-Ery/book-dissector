"""运行产物的原子文本与 JSON 写入。"""
from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any

from pydantic import BaseModel


def atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(content, encoding="utf-8")
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def atomic_write_json(path: Path, value: Any) -> None:
    payload = value.model_dump(mode="json") if isinstance(value, BaseModel) else value
    atomic_write_text(
        path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    )

