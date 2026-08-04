"""拆解任务 API：创建蒸馏任务（后台线程）、查询进度、获取结果。

任务状态保存在内存中（单用户本地工具，服务重启后需重新发起）。
结果持久化到 storage/intermediate/ 供 M5 导出使用。
"""
from __future__ import annotations

import asyncio
import json
import threading
from dataclasses import dataclass, field

from fastapi import APIRouter, HTTPException

from .. import config
from ..core.distiller import distill_book
from ..models.domain import QualityStatus
from ..models.schemas import (
    DisassembleRequest,
    DistillResultOut,
    ChapterDistillOut,
    TaskStatus,
)

router = APIRouter(prefix="/api", tags=["tasks"])


@dataclass
class TaskState:
    book_id: str
    status: str = "pending"  # pending / running / done / error
    stage: str = ""
    current: int = 0
    total: int = 0
    error: str = ""
    result: DistillResultOut | None = None


_TASKS: dict[str, TaskState] = {}


@router.post("/books/{book_id}/disassemble", response_model=TaskStatus)
async def start_disassemble(book_id: str, req: DisassembleRequest) -> TaskStatus:
    # 前置校验：书存在且已提取
    txt_path = config.BOOKS_DIR / f"{book_id}.txt"
    meta_path = config.BOOKS_DIR / f"{book_id}.meta.json"
    if not txt_path.exists():
        raise HTTPException(status_code=404, detail="未找到书籍（可能未提取成功）")
    meta: dict = {}
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            meta = {}
    if meta.get("extract_status") != "ok":
        raise HTTPException(status_code=409, detail="书籍文本提取未完成或失败")

    if not config.get_api_key():
        raise HTTPException(status_code=400, detail="尚未配置 DeepSeek API Key，请先在设置中填写")

    if book_id in _TASKS and _TASKS[book_id].status in ("pending", "running"):
        raise HTTPException(status_code=409, detail="该书已有进行中的拆解任务")

    state = TaskState(book_id=book_id)
    _TASKS[book_id] = state

    def _progress(stage: str, current: int, total: int) -> None:
        state.stage = stage
        state.current = current
        state.total = total

    def _run() -> None:
        state.status = "running"
        try:
            result = asyncio.run(
                distill_book(
                    book_id,
                    book_type=req.book_type,
                    strength=req.strength,
                    progress=_progress,
                )
            )
            state.result = _to_out(result)
            _persist(book_id, result)
            state.status = _completion_status(result)
        except Exception as exc:
            state.status = "error"
            state.error = str(exc)

    threading.Thread(target=_run, daemon=True).start()
    return _to_status(state)


@router.get("/tasks/{book_id}", response_model=TaskStatus)
def task_status(book_id: str) -> TaskStatus:
    state = _TASKS.get(book_id)
    if state is None:
        raise HTTPException(status_code=404, detail="任务不存在（服务重启后任务会丢失）")
    return _to_status(state)


@router.get("/tasks/{book_id}/result", response_model=DistillResultOut)
def task_result(book_id: str) -> DistillResultOut:
    state = _TASKS.get(book_id)
    if state is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    if state.status == "error":
        raise HTTPException(status_code=500, detail=state.error or "拆解失败")
    if state.status not in ("done", "quality_failed") or state.result is None:
        raise HTTPException(status_code=409, detail="任务尚未完成")
    return state.result


def _completion_status(result) -> str:
    return (
        "done"
        if result.quality_report.status == QualityStatus.PASS
        else "quality_failed"
    )


def _to_status(state: TaskState) -> TaskStatus:
    return TaskStatus(
        task_id=state.book_id,
        status=state.status,
        stage=state.stage,
        current=state.current,
        total=state.total,
        error=state.error,
        message=state.stage,
    )


def _to_out(result) -> DistillResultOut:
    kept = 0.0
    if result.total_source_chars > 0:
        kept = round(result.total_output_chars / result.total_source_chars, 4)
    return DistillResultOut(
        book_title=result.book_title,
        book_type=result.book_type,
        strength=result.strength,
        final_text=result.final_text,
        chapters=[
            ChapterDistillOut(
                title=c.title,
                source_chars=c.source_chars,
                target_chars=c.target_chars,
                output_chars=c.output_chars,
                error=c.error,
                unit_id=c.unit_id,
            )
            for c in result.chapters
        ],
        total_source_chars=result.total_source_chars,
        total_output_chars=result.total_output_chars,
        api_calls=result.api_calls,
        errors=result.errors,
        kept_ratio=kept,
        knowledge_units=result.knowledge_units,
        anchor_coverage=result.anchor_coverage,
        unit_coverage=result.unit_coverage,
        duplicate_merged_count=result.duplicate_merged_count,
        quality_report=result.quality_report,
        orientation_scan=result.orientation_scan,
        budget_plan=result.budget_plan,
    )


def _persist(book_id: str, result) -> None:
    config.ensure_dirs()
    (config.INTERMEDIATE_DIR / f"{book_id}.distilled.md").write_text(
        result.final_text, encoding="utf-8"
    )
    (config.INTERMEDIATE_DIR / f"{book_id}.distill.json").write_text(
        json.dumps(
            {
                "book_title": result.book_title,
                "book_type": result.book_type,
                "strength": result.strength,
                "total_source_chars": result.total_source_chars,
                "total_output_chars": result.total_output_chars,
                "api_calls": result.api_calls,
                "errors": result.errors,
                "anchor_coverage": result.anchor_coverage,
                "unit_coverage": result.unit_coverage.model_dump(mode="json"),
                "distill_units": [
                    unit.model_dump(mode="json") for unit in result.distill_units
                ],
                "knowledge_units": [
                    unit.model_dump(mode="json") for unit in result.knowledge_units
                ],
                "duplicate_merged_count": result.duplicate_merged_count,
                "quality_report": result.quality_report.model_dump(mode="json"),
                "model": config.DEEPSEEK_MODEL,
                "prompt_version": "1.0",
                "orientation_scan": (
                    result.orientation_scan.model_dump(mode="json")
                    if result.orientation_scan
                    else None
                ),
                "budget_plan": (
                    result.budget_plan.model_dump(mode="json")
                    if result.budget_plan
                    else None
                ),
                "chapters": [
                    {
                        "title": c.title,
                        "source_chars": c.source_chars,
                        "target_chars": c.target_chars,
                        "output_chars": c.output_chars,
                        "error": c.error,
                        "unit_id": c.unit_id,
                    }
                    for c in result.chapters
                ],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    (config.INTERMEDIATE_DIR / f"{book_id}.quality.json").write_text(
        json.dumps(
            result.quality_report.model_dump(mode="json"),
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
