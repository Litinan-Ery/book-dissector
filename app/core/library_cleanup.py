"""书库条目的可恢复删除与启动恢复。"""
from __future__ import annotations

import json
import os
import re
import uuid
from dataclasses import dataclass
from pathlib import Path

from .. import config
from .task_store import BookDeletionRecord, TaskStore


RESOURCE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")

WILL_DELETE = [
    "应用保存的导入副本",
    "提取文本与元数据",
    "删减和蒸馏中间产物",
    "书籍级缓存",
    "已取消、失败和已完成的关联任务",
]
WILL_KEEP = [
    "导入来源中的原始文件",
    "既有精华 Markdown 导出",
    "MyDatabase 中已有内容",
]


@dataclass
class BookDeletionOutcome:
    book_id: str
    already_absent: bool = False


def validate_resource_id(value: str) -> str:
    if not RESOURCE_ID_RE.fullmatch(value):
        raise ValueError("非法资源 ID")
    return value


def recycle_root() -> Path:
    return Path(config.STORAGE_DIR) / "recycle" / "books"


def managed_book_paths(book_id: str) -> list[Path]:
    """只枚举应用活动目录内属于该 book_id 的受管文件。"""
    validate_resource_id(book_id)
    config.ensure_dirs()
    paths: list[Path] = []
    books_dir = Path(config.BOOKS_DIR)
    intermediate_dir = Path(config.INTERMEDIATE_DIR)
    for path in books_dir.iterdir():
        if not path.is_file() or path.name.startswith("."):
            continue
        if path.name in {f"{book_id}.txt", f"{book_id}.meta.json"}:
            paths.append(path)
        elif path.name.startswith(f"{book_id}_"):
            paths.append(path)
    for path in intermediate_dir.iterdir():
        if path.is_file() and path.name.startswith(f"{book_id}."):
            paths.append(path)
    return sorted(set(paths), key=lambda item: str(item))


def book_exists(book_id: str) -> bool:
    return bool(managed_book_paths(book_id))


def _manifest_for(book_id: str, recycle_path: Path) -> list[dict[str, str]]:
    manifest: list[dict[str, str]] = []
    books_dir = Path(config.BOOKS_DIR).absolute()
    intermediate_dir = Path(config.INTERMEDIATE_DIR).absolute()
    for source in managed_book_paths(book_id):
        absolute = source.absolute()
        if absolute.parent == books_dir:
            bucket = "books"
        elif absolute.parent == intermediate_dir:
            bucket = "intermediate"
        else:
            raise ValueError(f"受管文件越界：{source}")
        manifest.append(
            {
                "source": str(absolute),
                "target": str((recycle_path / bucket / source.name).absolute()),
            }
        )
    return manifest


def _write_manifest(recycle_path: Path, manifest: list[dict[str, str]]) -> None:
    recycle_path.mkdir(parents=True, exist_ok=False)
    destination = recycle_path / "manifest.json"
    temporary = recycle_path / ".manifest.json.tmp"
    temporary.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(temporary, destination)


def _stage_files(manifest: list[dict[str, str]]) -> None:
    for item in manifest:
        source = Path(item["source"])
        target = Path(item["target"])
        target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(source, target)


def _restore_files(manifest: list[dict[str, str]]) -> None:
    errors: list[str] = []
    for item in reversed(manifest):
        source = Path(item["source"])
        target = Path(item["target"])
        if not target.exists():
            continue
        if source.exists():
            errors.append(f"原位置已存在，无法恢复：{source}")
            continue
        try:
            source.parent.mkdir(parents=True, exist_ok=True)
            os.replace(target, source)
        except OSError as exc:
            errors.append(f"恢复 {source.name} 失败：{exc}")
    if errors:
        raise OSError("；".join(errors))


def delete_book(book_id: str, store: TaskStore) -> BookDeletionOutcome:
    validate_resource_id(book_id)
    existing = store.get_book_deletion(book_id)
    if existing and existing.state == "completed":
        return BookDeletionOutcome(book_id=book_id, already_absent=True)
    paths = managed_book_paths(book_id)
    if not paths:
        raise FileNotFoundError("书籍不存在")

    recycle_path = recycle_root() / f"{book_id}-{uuid.uuid4().hex[:12]}"
    manifest = _manifest_for(book_id, recycle_path)
    state = store.prepare_book_deletion(book_id, str(recycle_path), manifest)
    if state == "completed":
        return BookDeletionOutcome(book_id=book_id, already_absent=True)
    if state != "preparing":
        raise RuntimeError("书籍删除正在恢复，请稍后重试")

    try:
        _write_manifest(recycle_path, manifest)
        _stage_files(manifest)
        store.mark_book_deletion_staged(book_id)
        store.finalize_book_deletion(book_id)
    except Exception as exc:
        try:
            _restore_files(manifest)
            store.abort_book_deletion(book_id)
        except Exception as restore_exc:
            store.fail_book_deletion(book_id, f"{exc}；回滚失败：{restore_exc}")
        raise
    return BookDeletionOutcome(book_id=book_id)


def recover_incomplete_book_deletions(store: TaskStore) -> int:
    """启动时回滚未提交完成的跨文件/数据库删除。"""
    recovered = 0
    for record in store.list_incomplete_book_deletions():
        try:
            _restore_record(record)
            store.abort_book_deletion(record.book_id)
            recovered += 1
        except Exception as exc:
            store.fail_book_deletion(record.book_id, f"启动恢复失败：{exc}")
    return recovered


def _restore_record(record: BookDeletionRecord) -> None:
    _restore_files(record.manifest)
