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

from fastapi import APIRouter, File, UploadFile
from fastapi.responses import JSONResponse

from .. import config
from ..core.extractors.base import extract_book
from ..models.schemas import BookInfo

router = APIRouter(prefix="/api/books", tags=["books"])

ALLOWED_EXTENSIONS = {".epub", ".pdf", ".txt", ".md"}
# 提取相关附属文件的后缀（列表时需要跳过）
META_SUFFIX = ".meta.json"
TEXT_SUFFIX = ".txt"


def _load_meta(book_id: str) -> dict:
    meta_path = config.BOOKS_DIR / f"{book_id}{META_SUFFIX}"
    if meta_path.exists():
        try:
            return json.loads(meta_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


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

    # 后台线程执行提取，不阻塞上传响应
    def _run() -> None:
        try:
            extract_book(book_id, dest)
        except Exception as exc:  # 提取失败也要落盘错误状态
            meta = {
                "book_id": book_id,
                "source_format": ext,
                "title": "",
                "author": "",
                "word_count": 0,
                "chapters": [],
                "extract_status": "error",
                "extract_error": f"提取异常：{exc.__class__.__name__}: {exc}",
            }
            (config.BOOKS_DIR / f"{book_id}{META_SUFFIX}").write_text(
                json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
            )

    threading.Thread(target=_run, daemon=True).start()

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
            )
        )
    return items


def ext_of(filename: str) -> str:
    return filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
