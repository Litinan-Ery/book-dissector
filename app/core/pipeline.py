"""校验 → 删减 → 定向扫描/预算 → 提炼 → 质量 → 导出的端到端流水线。"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from time import monotonic

from .. import config
from ..models.domain import QualityStatus, RunStatus
from .artifacts import ArtifactStore
from .atomic import atomic_write_json, atomic_write_text
from .distiller import distill_book
from .execution import DistillCancelled, TaskExecutionContext
from .exporter import export_book
from .extractors.base import Chapter
from .pruner import prune
from .result_store import persist_distill_result
from .runs import create_run
from .task_store import TaskStore


@dataclass
class BookDraft:
    book_id: str
    title: str
    source_format: str
    run_id: str = ""
    raw_text: str = ""
    pruned_text: str = ""
    distilled_text: str = ""
    output_path: str = ""
    quality_status: str = ""


def _load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _source_path(book_id: str) -> Path:
    candidates = [
        path
        for path in config.BOOKS_DIR.glob(f"{book_id}_*")
        if path.is_file()
    ]
    if candidates:
        return sorted(candidates)[0]
    extracted = config.BOOKS_DIR / f"{book_id}.txt"
    if extracted.exists():
        return extracted
    raise FileNotFoundError("未找到书籍原文件或提取文本")


def _prune_payload(result) -> dict:
    return {
        "original_chars": result.original_chars,
        "removed_chars": result.removed_chars,
        "kept_ratio": result.kept_ratio,
        "regions": [
            {
                "start": region.start,
                "end": region.end,
                "reason": region.reason,
                "label": region.label,
            }
            for region in result.regions
        ],
        "evidence_regions": [
            {
                "start": region.start,
                "end": region.end,
                "reason": region.reason,
                "label": region.label,
            }
            for region in result.evidence_regions
        ],
        "pruned_chapters": [
            {
                "title": chapter.title,
                "level": chapter.level,
                "start_char": chapter.start_char,
                "end_char": chapter.end_char,
            }
            for chapter in result.pruned_chapters
        ],
        "span_map": [entry.model_dump(mode="json") for entry in result.span_map],
        "span_map_report": result.span_map_report.model_dump(mode="json"),
    }


async def run_pipeline(
    book_id: str,
    *,
    book_type: str = "general",
    strength: str = "standard",
    task_store: TaskStore | None = None,
    task_id: str | None = None,
    use_fake: bool | None = None,
) -> BookDraft:
    started = monotonic()
    meta = _load_json(config.BOOKS_DIR / f"{book_id}.meta.json")
    if meta.get("extract_status") != "ok":
        raise RuntimeError("书籍文本提取未完成或失败")
    raw_path = config.BOOKS_DIR / f"{book_id}.txt"
    if not raw_path.exists():
        raise FileNotFoundError("未找到提取后的书籍文本")
    raw_text = raw_path.read_text(encoding="utf-8")
    artifact_store = ArtifactStore(config.RUNS_DIR)
    manifest = create_run(
        book_id=book_id,
        source_path=_source_path(book_id),
        store=artifact_store,
        book_type=book_type,
        strength=strength,
    )
    manifest.status = RunStatus.RUNNING
    manifest.current_stage = "validate"
    artifact_store.write_json(manifest.run_id, "manifest.json", manifest)

    def update(stage: str, current: int, total: int, message: str) -> None:
        if task_store and task_id:
            task_store.update_task(
                task_id,
                status="running",
                stage=stage,
                current=current,
                total=total,
                message=message,
                run_id=manifest.run_id,
            )

    try:
        update("validate", 0, 6, "校验提取结构与正文覆盖")
        artifact_store.write_json(manifest.run_id, "source/meta.json", meta)

        update("prune", 1, 6, "识别无关内容并建立可逆映射")
        chapters = [
            Chapter(
                item.get("title", ""),
                item.get("level", 1),
                item.get("start_char", 0),
                item.get("end_char", len(raw_text)),
            )
            for item in meta.get("chapters", [])
        ]
        prune_result = prune(raw_text, chapters)
        prune_payload = _prune_payload(prune_result)
        atomic_write_text(
            config.INTERMEDIATE_DIR / f"{book_id}.pruned.txt",
            prune_result.pruned_text,
        )
        atomic_write_json(
            config.INTERMEDIATE_DIR / f"{book_id}.prune.json", prune_payload
        )
        artifact_store.write_text(
            manifest.run_id, "prune/pruned.txt", prune_result.pruned_text
        )
        artifact_store.write_json(
            manifest.run_id, "prune/result.json", prune_payload
        )
        prune_hash = hashlib.sha256(
            json.dumps(prune_payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()

        execution = None
        if task_store and task_id:
            execution = TaskExecutionContext(
                store=task_store,
                task_id=task_id,
                source_fingerprint=meta.get("source_fingerprint")
                or manifest.source_fingerprint,
                prune_config_hash=prune_hash,
                book_type=book_type,
                strength=strength,
                model=config.DEEPSEEK_MODEL,
                prompt_version=manifest.prompt_version,
            )

        def distill_progress(message: str, current: int, total: int) -> None:
            update("distill", 2 + current, max(3, 2 + total), message)

        update("orientation", 2, 6, "执行全书定向扫描与预算分配")
        distill_result = await distill_book(
            book_id,
            book_type=book_type,
            strength=strength,
            progress=distill_progress,
            use_fake=use_fake,
            execution=execution,
        )
        distill_payload = persist_distill_result(book_id, distill_result)
        artifact_store.write_text(
            manifest.run_id, "distill/result.md", distill_result.final_text
        )
        artifact_store.write_json(
            manifest.run_id, "distill/result.json", distill_payload
        )
        artifact_store.write_json(
            manifest.run_id,
            "quality/report.json",
            distill_result.quality_report,
        )

        update("quality", 5, 6, "执行全书去重与质量门禁")
        output_path = ""
        if distill_result.quality_report.status == QualityStatus.PASS:
            update("export", 6, 6, "生成正式精华稿")
            exported = export_book(book_id)
            output_path = str(exported)
            artifact_store.write_text(
                manifest.run_id,
                "export/result.md",
                exported.read_text(encoding="utf-8"),
            )
            final_task_status = "done"
            manifest.status = RunStatus.COMPLETED
        else:
            final_task_status = "quality_failed"
            manifest.status = RunStatus.QUALITY_FAILED
        manifest.current_stage = "export" if output_path else "quality"
        artifact_store.write_json(manifest.run_id, "manifest.json", manifest)
        metrics = {
            "elapsed_seconds": round(monotonic() - started, 3),
            "api_calls": distill_result.api_calls,
            "cache_hits": distill_result.cache_hits,
            "input_tokens": distill_result.input_tokens,
            "output_tokens": distill_result.output_tokens,
            "prompt_cache_hit_tokens": distill_result.prompt_cache_hit_tokens,
            "prompt_cache_miss_tokens": distill_result.prompt_cache_miss_tokens,
            "actual_cost_cny": distill_result.actual_cost_cny,
        }
        if task_store and task_id:
            current_task = task_store.get_task(task_id)
            estimate = current_task.estimate if current_task else {}
            estimated_total_tokens = int(estimate.get("input_tokens", 0)) + int(
                estimate.get("output_tokens", 0)
            )
            actual_total_tokens = (
                distill_result.input_tokens + distill_result.output_tokens
            )
            metrics["token_estimate_error"] = (
                round(
                    abs(actual_total_tokens - estimated_total_tokens)
                    / actual_total_tokens,
                    4,
                )
                if actual_total_tokens
                else 0.0
            )
            estimated_time_mid = (
                float(estimate.get("time_seconds_low", 0))
                + float(estimate.get("time_seconds_high", 0))
            ) / 2
            metrics["time_estimate_error"] = (
                round(
                    abs(metrics["elapsed_seconds"] - estimated_time_mid)
                    / metrics["elapsed_seconds"],
                    4,
                )
                if metrics["elapsed_seconds"]
                else 0.0
            )
        if task_store and task_id:
            task_store.update_task(
                task_id,
                status=final_task_status,
                stage=manifest.current_stage,
                current=6,
                total=6,
                message=("拆解完成" if output_path else "质量门禁未通过"),
                result={
                    "run_id": manifest.run_id,
                    "quality_status": distill_result.quality_report.status.value,
                    "output_path": output_path,
                },
                metrics=metrics,
            )
        return BookDraft(
            book_id=book_id,
            title=meta.get("title") or book_id,
            source_format=meta.get("source_format", ""),
            run_id=manifest.run_id,
            raw_text=raw_text,
            pruned_text=prune_result.pruned_text,
            distilled_text=distill_result.final_text,
            output_path=output_path,
            quality_status=distill_result.quality_report.status.value,
        )
    except DistillCancelled:
        manifest.status = RunStatus.CANCELLED
        manifest.current_stage = "cancelled"
        artifact_store.write_json(manifest.run_id, "manifest.json", manifest)
        if task_store and task_id:
            task_store.update_task(
                task_id,
                status="cancelled",
                stage="cancelled",
                message="任务已取消，已完成检查点已保留",
            )
        raise
    except Exception as exc:
        manifest.status = RunStatus.FAILED
        manifest.current_stage = "error"
        artifact_store.write_json(manifest.run_id, "manifest.json", manifest)
        if task_store and task_id:
            task_store.update_task(
                task_id,
                status="error",
                stage="error",
                error=str(exc),
                message="流水线执行失败",
            )
        raise
