"""导出 API：预览导出内容、生成导出文件、下载与列表。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse

from .. import config
from ..core.exporter import ExportQualityError, build_export_md, export_book
from ..models.schemas import ExportResultOut, ExportInfo

router = APIRouter(prefix="/api", tags=["export"])


@router.get("/books/{book_id}/export/preview")
def preview_export(book_id: str) -> PlainTextResponse:
    """预览导出内容（元信息头 + 精华正文）。"""
    try:
        return PlainTextResponse(build_export_md(book_id))
    except (FileNotFoundError, ExportQualityError) as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.post("/books/{book_id}/export", response_model=ExportResultOut)
def do_export(book_id: str) -> ExportResultOut:
    """生成导出文件（不覆盖历史版本）。"""
    try:
        dest = export_book(book_id)
    except (FileNotFoundError, ExportQualityError) as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return ExportResultOut(
        filename=dest.name,
        path=str(dest),
        size_bytes=dest.stat().st_size,
    )


@router.post("/books/{book_id}/export/diagnostic", response_model=ExportResultOut)
def do_diagnostic_export(book_id: str) -> ExportResultOut:
    """质量未通过时生成带醒目标记的诊断稿。"""
    try:
        dest = export_book(book_id, diagnostic=True)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return ExportResultOut(
        filename=dest.name,
        path=str(dest),
        size_bytes=dest.stat().st_size,
    )


@router.get("/outputs", response_model=list[ExportInfo])
def list_exports() -> list[ExportInfo]:
    config.ensure_dirs()
    items: list[ExportInfo] = []
    for path in sorted(config.OUTPUT_DIR.iterdir()):
        if path.is_file() and path.suffix.lower() == ".md":
            items.append(
                ExportInfo(
                    filename=path.name,
                    size_bytes=path.stat().st_size,
                    created_at=path.stat().st_mtime,
                )
            )
    return items


@router.get("/outputs/{filename}")
def download_export(filename: str) -> FileResponse:
    """下载导出文件。"""
    # 防目录穿越：仅允许文件名
    if "/" in filename or "\\" in filename or filename.startswith("."):
        raise HTTPException(status_code=400, detail="非法文件名")
    path = config.OUTPUT_DIR / filename
    if not path.exists():
        raise HTTPException(status_code=404, detail="文件不存在")
    return FileResponse(
        path,
        media_type="text/markdown; charset=utf-8",
        filename=filename,
    )
