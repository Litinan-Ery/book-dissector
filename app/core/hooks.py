"""Post-export hooks for moving finished artifacts into local systems."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from .. import config


class PostExportHookError(RuntimeError):
    """A configured post-export delivery failed after the export was written."""


@dataclass(frozen=True)
class MyDatabaseHookResult:
    enabled: bool
    entry_id: str = ""
    entry_action: str = ""
    note_action: str = ""
    note_path: str = ""


def run_post_export_hooks(
    output_path: Path,
    *,
    book_id: str,
    book_meta: dict,
) -> MyDatabaseHookResult:
    """Run configured hooks synchronously so failures are never reported as success."""
    settings = config.load_config().get("mydatabase_hook", {})
    if not isinstance(settings, dict) or not settings.get("enabled", False):
        return MyDatabaseHookResult(enabled=False)

    project_root = _required_path(settings, "project_root")
    database_path = _configured_path(settings, "database_path", project_root)
    vault_path = _configured_path(settings, "vault_path", project_root)
    if database_path is None:
        database_path = project_root / "data" / "mydatabase.sqlite3"
    if vault_path is None:
        vault_path = project_root / "vault"
    source_root = project_root / "src"
    if not source_root.is_dir():
        raise PostExportHookError(
            "精华已生成，但 MyDatabase hook 配置无效：缺少 {}".format(source_root)
        )
    if not database_path.is_file():
        raise PostExportHookError(
            "精华已生成，但 MyDatabase 数据库不存在：{}".format(database_path)
        )

    fingerprint = _source_fingerprint(book_id, book_meta)
    command = [
        sys.executable,
        "-m",
        "mydatabase",
        "import-book-summary",
        "--db",
        str(database_path),
        "--vault",
        str(vault_path),
        "--summary-file",
        str(Path(output_path).resolve()),
        "--title",
        str(book_meta.get("title") or book_id),
        "--source-fingerprint",
        fingerprint,
        "--source-book-id",
        book_id,
    ]
    author = str(book_meta.get("author") or "").strip()
    if author:
        command.extend(["--author", author])

    environment = os.environ.copy()
    existing_pythonpath = environment.get("PYTHONPATH", "")
    environment["PYTHONPATH"] = str(source_root) + (
        os.pathsep + existing_pythonpath if existing_pythonpath else ""
    )
    timeout_seconds = settings.get("timeout_seconds", 30)
    try:
        timeout = max(1.0, float(timeout_seconds))
    except (TypeError, ValueError):
        raise PostExportHookError(
            "精华已生成，但 MyDatabase hook 的 timeout_seconds 无效"
        )

    try:
        completed = subprocess.run(
            command,
            cwd=project_root,
            env=environment,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PostExportHookError(
            "精华已生成，但 MyDatabase 导入未完成：{}".format(exc)
        ) from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "未知错误").strip()
        raise PostExportHookError(
            "精华已生成，但 MyDatabase 导入失败：{}".format(detail[:1000])
        )
    try:
        payload = json.loads(completed.stdout)
    except (json.JSONDecodeError, TypeError) as exc:
        raise PostExportHookError(
            "精华已生成，但 MyDatabase 返回了无法识别的结果"
        ) from exc
    return MyDatabaseHookResult(
        enabled=True,
        entry_id=str(payload.get("entry_id", "")),
        entry_action=str(payload.get("entry_action", "")),
        note_action=str(payload.get("note_action", "")),
        note_path=str(payload.get("note_path", "")),
    )


def _required_path(settings: dict, key: str) -> Path:
    value = settings.get(key)
    if not isinstance(value, str) or not value.strip():
        raise PostExportHookError(
            "精华已生成，但 MyDatabase hook 缺少配置项：{}".format(key)
        )
    return Path(value).expanduser().resolve()


def _configured_path(settings: dict, key: str, project_root: Path) -> Path | None:
    value = settings.get(key)
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise PostExportHookError(
            "精华已生成，但 MyDatabase hook 配置项 {} 必须是路径".format(key)
        )
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = project_root / path
    return path.resolve()


def _source_fingerprint(book_id: str, book_meta: dict) -> str:
    existing = str(book_meta.get("source_fingerprint") or "").strip().lower()
    if len(existing) == 64 and all(character in "0123456789abcdef" for character in existing):
        return existing
    candidates = sorted(
        path for path in config.BOOKS_DIR.glob("{}_*".format(book_id)) if path.is_file()
    )
    if not candidates:
        extracted = config.BOOKS_DIR / "{}.txt".format(book_id)
        if extracted.is_file():
            candidates = [extracted]
    if not candidates:
        raise PostExportHookError(
            "精华已生成，但无法计算原书指纹：未找到书籍源文件"
        )
    digest = hashlib.sha256()
    with candidates[0].open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
