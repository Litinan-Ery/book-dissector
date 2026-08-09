"""第一版体验的一键流水线：删减 → 压缩 → 导出。"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .. import config
from .distiller import DistillCancelled, DistillInterrupted, DistillResult, distill_book
from .exporter import export_book
from .extractors.base import Chapter
from .pruner import prune
from .task_store import TaskStore


@dataclass
class BookDraft:
    book_id: str
    title: str
    source_format: str
    raw_text: str = ""
    pruned_text: str = ""
    distilled_text: str = ""
    output_path: str = ""


def _load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def persist_distill_result(book_id: str, result: DistillResult) -> dict:
    config.ensure_dirs()
    payload = {
        "book_title": result.book_title,
        "book_type": result.book_type,
        "strength": result.strength,
        "final_text": result.final_text,
        "total_source_chars": result.total_source_chars,
        "total_output_chars": result.total_output_chars,
        "api_calls": result.api_calls,
        "cache_hits": result.cache_hits,
        "errors": result.errors,
        "modality_warnings": result.modality_warnings,
        "chapters": [
            {
                "title": chapter.title,
                "source_chars": chapter.source_chars,
                "target_chars": chapter.target_chars,
                "output_chars": chapter.output_chars,
                "error": chapter.error,
            }
            for chapter in result.chapters
        ],
    }
    (config.INTERMEDIATE_DIR / f"{book_id}.distilled.md").write_text(
        result.final_text, encoding="utf-8"
    )
    (config.INTERMEDIATE_DIR / f"{book_id}.distill.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return payload


async def run_pipeline(
    book_id: str,
    *,
    book_type: str = "general",
    strength: str = "standard",
    task_store: TaskStore | None = None,
    task_id: str | None = None,
    use_fake: bool | None = None,
    should_interrupt=None,
) -> BookDraft:
    meta = _load_json(config.BOOKS_DIR / f"{book_id}.meta.json")
    raw_path = config.BOOKS_DIR / f"{book_id}.txt"
    if meta.get("extract_status") != "ok" or not raw_path.exists():
        raise RuntimeError("书籍文本提取未完成或失败")
    raw_text = raw_path.read_text(encoding="utf-8")

    def update(stage: str, current: int, total: int, message: str, **extra) -> None:
        if task_store and task_id:
            task_store.update_task(
                task_id,
                status="running",
                stage=stage,
                current=current,
                total=total,
                message=message,
                **extra,
            )

    def ensure_can_continue() -> None:
        if task_store and task_id and task_store.is_cancel_requested(task_id):
            raise DistillCancelled("任务已停止，未创建新的模型请求")
        if should_interrupt and should_interrupt():
            raise DistillInterrupted("服务正在关闭，已保存安全边界")

    try:
        ensure_can_continue()
        update("prune", 1, 4, "识别并删减无关内容")
        chapters = [
            Chapter(
                item.get("title", ""),
                item.get("level", 1),
                item.get("start_char", 0),
                item.get("end_char", len(raw_text)),
            )
            for item in meta.get("chapters", [])
        ]
        pruned = prune(raw_text, chapters)
        (config.INTERMEDIATE_DIR / f"{book_id}.pruned.txt").write_text(
            pruned.pruned_text, encoding="utf-8"
        )
        (config.INTERMEDIATE_DIR / f"{book_id}.prune.json").write_text(
            json.dumps(
                {
                    "original_chars": pruned.original_chars,
                    "removed_chars": pruned.removed_chars,
                    "kept_ratio": pruned.kept_ratio,
                    "regions": [region.__dict__ for region in pruned.regions],
                    "pruned_chapters": [chapter.__dict__ for chapter in pruned.pruned_chapters],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        ensure_can_continue()

        def progress(message: str, current: int, total: int) -> None:
            update("distill", current, total, message)

        update("distill", 2, 4, "压缩核心章节并提取精华")
        result = await distill_book(
            book_id,
            book_type=book_type,
            strength=strength,
            progress=progress,
            use_fake=use_fake,
            task_store=task_store,
            task_id=task_id,
            should_interrupt=should_interrupt,
        )
        payload = persist_distill_result(book_id, result)
        if result.errors:
            if task_store and task_id:
                task_store.update_task(
                    task_id,
                    status="error",
                    stage="distill",
                    error="；".join(result.errors),
                    message="部分处理单元失败，可仅重试失败单元",
                    result=payload,
                )
            return BookDraft(
                book_id=book_id,
                title=meta.get("title") or book_id,
                source_format=meta.get("source_format", ""),
                raw_text=raw_text,
                pruned_text=pruned.pruned_text,
                distilled_text=result.final_text,
            )

        ensure_can_continue()
        update("export", 4, 4, "导出精华 Markdown")
        output_path = str(export_book(book_id))
        if task_store and task_id:
            task_store.update_task(
                task_id,
                status="done",
                stage="export",
                current=4,
                total=4,
                message="拆解完成",
                result={**payload, "output_path": output_path},
                metrics={"api_calls": result.api_calls, "cache_hits": result.cache_hits},
            )
        return BookDraft(
            book_id=book_id,
            title=meta.get("title") or book_id,
            source_format=meta.get("source_format", ""),
            raw_text=raw_text,
            pruned_text=pruned.pruned_text,
            distilled_text=result.final_text,
            output_path=output_path,
        )
    except DistillCancelled:
        if task_store and task_id:
            task_store.update_task(
                task_id,
                status="cancelled",
                stage="cancelled",
                message="任务已取消，可稍后恢复",
            )
        raise
    except DistillInterrupted:
        # 保持 running 状态；下次启动会将它恢复为
        # pending，并从已持久化的检查点继续。
        raise
    except Exception as exc:
        if task_store and task_id:
            task_store.update_task(
                task_id,
                status="error",
                stage="error",
                error=str(exc),
                message="拆解失败",
            )
        raise
