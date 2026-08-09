"""书籍导入相关 API：上传保存到本地，后台线程执行文本提取。

上传后立即返回（extract_status=processing），提取结果
写入 {book_id}.txt 与 {book_id}.meta.json，列表接口合并展示。
"""
from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import JSONResponse

from .. import config
from ..core.extractors.base import extract_book
from ..core.library_cleanup import (
    WILL_DELETE,
    WILL_KEEP,
    book_exists,
    delete_book,
    validate_resource_id,
)
from ..core.task_store import ActiveBookTasksError
from ..models.schemas import BookDeletionPreview, BookInfo, DeletionResult

router = APIRouter(prefix="/api/books", tags=["books"])

ALLOWED_EXTENSIONS = {".epub", ".pdf", ".txt", ".md"}
# 提取相关附属文件的后缀（列表时需要跳过）
META_SUFFIX = ".meta.json"
TEXT_SUFFIX = ".txt"
_extraction_lock = threading.Lock()
_active_extractions: set[str] = set()


def is_extraction_active(book_id: str) -> bool:
    with _extraction_lock:
        return book_id in _active_extractions


def _task_store():
    from . import tasks as task_api

    return task_api._get_store()


def _load_meta(book_id: str) -> dict:
    meta_path = config.BOOKS_DIR / f"{book_id}{META_SUFFIX}"
    if meta_path.exists():
        try:
            return json.loads(meta_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _write_meta(book_id: str, payload: dict) -> None:
    (config.BOOKS_DIR / f"{book_id}{META_SUFFIX}").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _mark_processing(book_id: str, source_path: Path) -> None:
    meta = {
        "title": "",
        "author": "",
        "word_count": 0,
        "chapters": [],
        "modality_warnings": [],
        **_load_meta(book_id),
        "book_id": book_id,
        "source_format": source_path.suffix.lower().lstrip("."),
        "extract_status": "processing",
        "extract_error": "",
    }
    _write_meta(book_id, meta)


def _mark_extraction_error(book_id: str, source_path: Path, exc: Exception) -> None:
    meta = {
        "title": "",
        "author": "",
        "word_count": 0,
        "chapters": [],
        "modality_warnings": [],
        **_load_meta(book_id),
        "book_id": book_id,
        "source_format": source_path.suffix.lower().lstrip("."),
        "extract_status": "error",
        "extract_error": f"提取异常：{exc.__class__.__name__}: {exc}",
    }
    _write_meta(book_id, meta)


def _run_extraction(book_id: str, source_path: Path) -> None:
    try:
        extract_book(book_id, source_path)
    except Exception as exc:
        _mark_extraction_error(book_id, source_path, exc)
    finally:
        with _extraction_lock:
            _active_extractions.discard(book_id)


def _start_extraction(book_id: str, source_path: Path) -> bool:
    with _extraction_lock:
        if book_id in _active_extractions:
            return False
        _mark_processing(book_id, source_path)
        _active_extractions.add(book_id)
    thread = threading.Thread(
        target=_run_extraction,
        args=(book_id, source_path),
        daemon=True,
        name=f"book-extract-{book_id}",
    )
    try:
        thread.start()
    except Exception:
        with _extraction_lock:
            _active_extractions.discard(book_id)
        raise
    return True


def recover_incomplete_extractions() -> int:
    """服务重启后，重新执行尚未产出文本的上传提取任务。"""
    config.ensure_dirs()
    recovered = 0
    for source_path in sorted(config.BOOKS_DIR.iterdir()):
        if not source_path.is_file() or source_path.name.startswith("."):
            continue
        if source_path.name.endswith(META_SUFFIX) or "_" not in source_path.name:
            continue
        book_id, _, _original_name = source_path.name.partition("_")
        meta = _load_meta(book_id)
        status = meta.get("extract_status", "pending")
        text_exists = (config.BOOKS_DIR / f"{book_id}{TEXT_SUFFIX}").exists()
        if status not in {"pending", "processing"} or (status == "pending" and text_exists):
            continue
        try:
            if _start_extraction(book_id, source_path):
                recovered += 1
        except Exception as exc:
            _mark_extraction_error(book_id, source_path, exc)
    return recovered


@router.post("/upload", response_model=BookInfo)
async def upload_book(file: UploadFile = File(...)) -> BookInfo:
    filename = file.filename or "unknown"
    ext = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext not in ALLOWED_EXTENSIONS:
        return JSONResponse(
            status_code=415,
            content={"detail": f"不支持的格式：{ext or '（无扩展名）'}，"
                     f"支持：{', '.join(sorted(ALLOWED_EXTENSIONS))}"},
        )

    config.ensure_dirs()
    book_id = uuid.uuid4().hex[:12]
    dest = config.BOOKS_DIR / f"{book_id}_{filename}"

    size = 0
    with dest.open("wb") as out:
        while chunk := await file.read(1024 * 1024):
            out.write(chunk)
            size += len(chunk)

    # 先持久化状态，再在后台提取；页面刷新和服务重启都能看到真实进度。
    try:
        _start_extraction(book_id, dest)
    except Exception as exc:
        _mark_extraction_error(book_id, dest, exc)
        raise

    return BookInfo(
        id=book_id,
        filename=filename,
        size_bytes=size,
        uploaded_at=datetime.now(timezone.utc),
        extract_status="processing",
        extract_error="",
        title="",
        author="",
        source_format=ext.lstrip("."),
        word_count=0,
    )


@router.get("", response_model=list[BookInfo])
def list_books() -> list[BookInfo]:
    config.ensure_dirs()
    items: list[BookInfo] = []
    for path in sorted(config.BOOKS_DIR.iterdir()):
        if not path.is_file() or path.name.startswith("."):
            continue
        if path.name.endswith(META_SUFFIX):
            continue
        # 提取产物 {book_id}.txt 没有下划线结构，跳过；原书格式为 {book_id}_{原始文件名}
        if "_" not in path.name:
            continue
        book_id, _, original_name = path.name.partition("_")
        meta = _load_meta(book_id)
        extract_status = meta.get("extract_status", "pending")
        if extract_status == "pending" and (config.BOOKS_DIR / f"{book_id}{TEXT_SUFFIX}").exists():
            extract_status = "ok"
        items.append(
            BookInfo(
                id=book_id,
                filename=original_name,
                size_bytes=path.stat().st_size,
                uploaded_at=datetime.fromtimestamp(
                    path.stat().st_mtime, tz=timezone.utc
                ),
                title=meta.get("title", ""),
                author=meta.get("author", ""),
                source_format=meta.get("source_format", ext_of(original_name)),
                word_count=meta.get("word_count", 0),
                extract_status=extract_status,
                extract_error=meta.get("extract_error", ""),
                modality_warnings=meta.get("modality_warnings", []),
            )
        )
    return items


@router.get("/{book_id}/deletion-preview", response_model=BookDeletionPreview)
def deletion_preview(book_id: str) -> BookDeletionPreview:
    try:
        validate_resource_id(book_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if not book_exists(book_id):
        raise HTTPException(status_code=404, detail="书籍不存在")
    meta = _load_meta(book_id)
    related = _task_store().list_tasks_for_book(book_id)
    active = [
        task.task_id
        for task in related
        if task.status in {"pending", "running"}
    ]
    return BookDeletionPreview(
        book_id=book_id,
        title=meta.get("title") or book_id,
        task_count=len(related),
        active_task_ids=active,
        will_delete=WILL_DELETE,
        will_keep=WILL_KEEP,
    )


@router.delete("/{book_id}", response_model=DeletionResult)
def remove_book(book_id: str) -> DeletionResult:
    try:
        validate_resource_id(book_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    meta = _load_meta(book_id)
    if is_extraction_active(book_id) or meta.get("extract_status") == "processing":
        raise HTTPException(status_code=409, detail="书籍正在提取，请完成后再删除")
    try:
        outcome = delete_book(book_id, _task_store())
    except ActiveBookTasksError as exc:
        ids = "、".join(exc.task_ids)
        raise HTTPException(
            status_code=409,
            detail=f"存在等待中或运行中的关联任务，请先删除：{ids}",
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"删除失败，已尝试回滚：{exc}")
    return DeletionResult(
        resource_id=book_id,
        state="deleted",
        message="书籍条目已删除" if not outcome.already_absent else "书籍条目已不存在",
        already_absent=outcome.already_absent,
    )


def ext_of(filename: str) -> str:
    return filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
