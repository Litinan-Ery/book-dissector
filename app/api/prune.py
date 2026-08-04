"""删减 API：执行无关内容删减、恢复误删区域、提供预览数据。

数据来源：storage/books/{book_id}.txt（全文）与 .meta.json（章节结构）。
结果持久化：storage/intermediate/{book_id}.pruned.txt + .prune.json
"""
from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import PlainTextResponse

from .. import config
from ..core.extractors.base import Chapter
from ..core.pruner import Region, prune
from ..models.schemas import PruneResultOut, PruneRegion, RestoreRequest

router = APIRouter(prefix="/api/books", tags=["prune"])


def _load_source(book_id: str) -> tuple[str, list[dict]]:
    """读取全文与章节结构；不存在或未提取成功则 404/409。"""
    txt_path = config.BOOKS_DIR / f"{book_id}.txt"
    meta_path = config.BOOKS_DIR / f"{book_id}.meta.json"
    if not txt_path.exists():
        raise HTTPException(status_code=404, detail="未找到书籍文本（可能未提取成功）")
    meta: dict = {}
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            meta = {}
    if meta.get("extract_status") != "ok":
        raise HTTPException(status_code=409, detail="书籍文本提取未完成或失败")
    text = txt_path.read_text(encoding="utf-8")
    chapters = [
        Chapter(
            title=c.get("title", ""),
            level=c.get("level", 1),
            start_char=c.get("start_char", 0),
            end_char=c.get("end_char", len(text)),
        )
        for c in meta.get("chapters", [])
    ]
    return text, chapters


def _save_result(book_id: str, result: PruneResultOut) -> None:
    config.ensure_dirs()
    (config.INTERMEDIATE_DIR / f"{book_id}.pruned.txt").write_text(
        result.pruned_text, encoding="utf-8"
    )
    (config.INTERMEDIATE_DIR / f"{book_id}.prune.json").write_text(
        json.dumps(
            {
                "original_chars": result.original_chars,
                "removed_chars": result.removed_chars,
                "kept_ratio": result.kept_ratio,
                "regions": [
                    {"start": r.start, "end": r.end, "reason": r.reason, "label": r.label}
                    for r in result.regions
                ],
                "pruned_chapters": [
                    {"title": c.title, "level": c.level, "start_char": c.start_char, "end_char": c.end_char}
                    for c in result.pruned_chapters
                ],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def _to_out(result) -> PruneResultOut:
    return PruneResultOut(
        pruned_text=result.pruned_text,
        regions=[
            PruneRegion(start=r.start, end=r.end, reason=r.reason, label=r.label)
            for r in result.regions
        ],
        original_chars=result.original_chars,
        removed_chars=result.removed_chars,
        kept_ratio=result.kept_ratio,
        pruned_chapters=[
            {"title": c.title, "level": c.level, "start_char": c.start_char, "end_char": c.end_char}
            for c in getattr(result, "pruned_chapters", [])
        ],
    )


@router.get("/{book_id}/prune/original")
def get_original_text(book_id: str) -> PlainTextResponse:
    """返回原始全文（预览对照用）。"""
    text, _ = _load_source(book_id)
    return PlainTextResponse(text)


@router.post("/{book_id}/prune", response_model=PruneResultOut)
def run_prune(book_id: str) -> PruneResultOut:
    text, chapters = _load_source(book_id)
    result = prune(text, chapters)
    out = _to_out(result)
    _save_result(book_id, out)
    return out


@router.post("/{book_id}/prune/restore", response_model=PruneResultOut)
def restore_regions(book_id: str, payload: RestoreRequest) -> PruneResultOut:
    text, chapters = _load_source(book_id)
    restore = [(r[0], r[1]) for r in payload.regions if len(r) == 2]
    result = prune(text, chapters, restore_regions=restore)
    out = _to_out(result)
    _save_result(book_id, out)
    return out


@router.get("/{book_id}/prune", response_model=PruneResultOut)
def get_prune_result(book_id: str) -> PruneResultOut:
    """读取上次删减结果（预览刷新用）。"""
    prune_path = config.INTERMEDIATE_DIR / f"{book_id}.prune.json"
    txt_path = config.INTERMEDIATE_DIR / f"{book_id}.pruned.txt"
    if not prune_path.exists() or not txt_path.exists():
        raise HTTPException(status_code=404, detail="尚未执行过删减")
    data = json.loads(prune_path.read_text(encoding="utf-8"))
    return PruneResultOut(
        pruned_text=txt_path.read_text(encoding="utf-8"),
        regions=[
            PruneRegion(start=r["start"], end=r["end"], reason=r["reason"], label=r["label"])
            for r in data.get("regions", [])
        ],
        original_chars=data.get("original_chars", 0),
        removed_chars=data.get("removed_chars", 0),
        kept_ratio=data.get("kept_ratio", 0.0),
    )
