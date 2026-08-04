"""SQLite 持久化的一键拆解任务、队列、取消、恢复与失败重试 API。"""
from __future__ import annotations

import asyncio
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi import APIRouter, HTTPException

from .. import config
from ..core.budget import STRENGTH_RATIOS, TYPE_FACTORS
from ..core.distiller import MAX_CHUNK_CHARS
from ..core.execution import DistillCancelled
from ..core.estimation import estimate_request
from ..core.pipeline import run_pipeline
from ..core.result_store import persist_distill_result
from ..core.task_store import StoredTask, TaskStore
from ..models.domain import QualityStatus
from ..models.schemas import (
    ChapterDistillOut,
    DisassembleRequest,
    DistillResultOut,
    MoveTaskRequest,
    TaskStatus,
)

router = APIRouter(prefix="/api", tags=["tasks"])

_runtime_lock = threading.Lock()
_active_tasks: set[str] = set()
_store: TaskStore | None = None
_store_path: Path | None = None
_executor: ThreadPoolExecutor | None = None
MAX_ACTIVE_BOOK_TASKS = 2


def _get_store() -> TaskStore:
    global _store, _store_path
    path = Path(config.TASK_DB)
    with _runtime_lock:
        if _store is None or _store_path != path:
            _store = TaskStore(path)
            _store_path = path
        return _store


def _get_executor() -> ThreadPoolExecutor:
    global _executor
    with _runtime_lock:
        if _executor is None:
            _executor = ThreadPoolExecutor(
                max_workers=2, thread_name_prefix="book-pipeline"
            )
        return _executor


def reset_runtime_for_tests() -> None:
    """清理惰性运行时；只供隔离测试和本地重载使用。"""
    global _store, _store_path, _executor
    with _runtime_lock:
        executor = _executor
        _executor = None
        _store = None
        _store_path = None
        _active_tasks.clear()
    if executor is not None:
        executor.shutdown(wait=True, cancel_futures=True)


def _estimate(book_id: str, book_type: str, strength: str) -> dict:
    text_path = config.BOOKS_DIR / f"{book_id}.txt"
    text = text_path.read_text(encoding="utf-8") if text_path.exists() else ""
    ratio = STRENGTH_RATIOS.get(strength, 0.15) * TYPE_FACTORS.get(book_type, 1.0)
    return estimate_request(
        text, target_ratio=ratio, max_chunk_chars=MAX_CHUNK_CHARS
    )


def _to_status_record(task: StoredTask) -> TaskStatus:
    return TaskStatus(
        task_id=task.task_id,
        book_id=task.book_id,
        run_id=task.run_id,
        status=task.status,
        stage=task.stage,
        current=task.current,
        total=task.total,
        error=task.error,
        message=task.message,
        estimate=task.estimate,
        metrics=task.metrics,
        result=task.result,
    )


def _run_task(task_id: str) -> None:
    store = _get_store()
    task = store.get_task(task_id)
    if task is None:
        return
    try:
        asyncio.run(
            run_pipeline(
                task.book_id,
                book_type=task.book_type,
                strength=task.strength,
                task_store=store,
                task_id=task.task_id,
            )
        )
    except DistillCancelled:
        pass
    except Exception:
        # run_pipeline 已把可展示错误持久化；线程不能吞掉任务状态。
        pass
    finally:
        with _runtime_lock:
            _active_tasks.discard(task_id)
        _schedule_pending()


def _schedule(task_id: str) -> bool:
    with _runtime_lock:
        if task_id in _active_tasks:
            return True
        if len(_active_tasks) >= MAX_ACTIVE_BOOK_TASKS:
            return False
        _active_tasks.add(task_id)
    _get_executor().submit(_run_task, task_id)
    return True


def _schedule_pending() -> None:
    """严格按 SQLite queue_order 填充空闲槽，移动队列后立即生效。"""
    for task in _get_store().list_tasks():
        if task.status != "pending" or task.cancel_requested:
            continue
        if not _schedule(task.task_id):
            break


def recover_and_schedule() -> int:
    store = _get_store()
    recovered = store.recover_interrupted()
    _schedule_pending()
    return recovered


@router.get("/books/{book_id}/estimate")
def estimate_disassemble(
    book_id: str,
    book_type: str = "general",
    strength: str = "standard",
) -> dict:
    if not (config.BOOKS_DIR / f"{book_id}.txt").exists():
        raise HTTPException(status_code=404, detail="未找到书籍文本")
    if book_type not in TYPE_FACTORS:
        raise HTTPException(status_code=400, detail="未知书籍类型")
    if strength not in STRENGTH_RATIOS:
        raise HTTPException(status_code=400, detail="未知压缩强度")
    return _estimate(book_id, book_type, strength)


@router.post("/books/{book_id}/disassemble", response_model=TaskStatus)
async def start_disassemble(book_id: str, req: DisassembleRequest) -> TaskStatus:
    text_path = config.BOOKS_DIR / f"{book_id}.txt"
    meta_path = config.BOOKS_DIR / f"{book_id}.meta.json"
    if not text_path.exists():
        raise HTTPException(status_code=404, detail="未找到书籍（可能未提取成功）")
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        meta = {}
    if meta.get("extract_status") != "ok":
        raise HTTPException(status_code=409, detail="书籍文本提取未完成或失败")
    if not req.cloud_consent:
        raise HTTPException(
            status_code=400,
            detail="开始前需确认：必要的正文片段会发送给 DeepSeek 云端提炼",
        )
    if not config.get_api_key():
        raise HTTPException(status_code=400, detail="尚未配置 DeepSeek API Key，请先填写")
    store = _get_store()
    if any(
        task.book_id == book_id and task.status in {"pending", "running"}
        for task in store.list_tasks()
    ):
        raise HTTPException(status_code=409, detail="该书已有等待中或运行中的任务")
    task = store.create_task(
        book_id,
        req.book_type,
        req.strength,
        estimate=_estimate(book_id, req.book_type, req.strength),
    )
    _schedule_pending()
    return _to_status_record(task)


@router.get("/tasks", response_model=list[TaskStatus])
def list_tasks() -> list[TaskStatus]:
    return [_to_status_record(task) for task in _get_store().list_tasks()]


@router.get("/tasks/{task_id}", response_model=TaskStatus)
def task_status(task_id: str) -> TaskStatus:
    task = _get_store().get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return _to_status_record(task)


@router.get("/tasks/{task_id}/result", response_model=DistillResultOut)
def task_result(task_id: str) -> DistillResultOut:
    task = _get_store().get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    if task.status == "error":
        raise HTTPException(status_code=500, detail=task.error or "拆解失败")
    if task.status not in {"done", "quality_failed"} or not task.run_id:
        raise HTTPException(status_code=409, detail="任务尚未完成")
    path = config.RUNS_DIR / task.run_id / "distill" / "result.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return DistillResultOut.model_validate(payload)
    except (FileNotFoundError, json.JSONDecodeError, ValueError) as exc:
        raise HTTPException(status_code=500, detail=f"任务结果损坏或缺失：{exc}")


@router.post("/tasks/{task_id}/cancel", response_model=TaskStatus)
def cancel_task(task_id: str) -> TaskStatus:
    store = _get_store()
    task = store.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    if task.status in {"done", "quality_failed", "error", "cancelled"}:
        raise HTTPException(status_code=409, detail="任务已结束，不能取消")
    store.request_cancel(task_id)
    updated = store.get_task(task_id)
    assert updated is not None
    return _to_status_record(updated)


@router.post("/tasks/{task_id}/retry-failed", response_model=TaskStatus)
def retry_failed(task_id: str) -> TaskStatus:
    store = _get_store()
    task = store.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    reset = store.reset_failed_units(task_id)
    if reset == 0:
        raise HTTPException(status_code=409, detail="没有失败单元可重试")
    store.update_task(
        task_id,
        status="pending",
        stage="resume",
        error="",
        message=f"等待重试 {reset} 个失败单元",
        cancel_requested=False,
    )
    _schedule_pending()
    updated = store.get_task(task_id)
    assert updated is not None
    return _to_status_record(updated)


@router.post("/tasks/{task_id}/resume", response_model=TaskStatus)
def resume_task(task_id: str) -> TaskStatus:
    store = _get_store()
    task = store.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    if task.status not in {"pending", "cancelled", "error", "quality_failed"}:
        raise HTTPException(status_code=409, detail="当前状态不能恢复")
    store.update_task(
        task_id,
        status="pending",
        stage="resume",
        error="",
        message="等待从检查点恢复",
        cancel_requested=False,
    )
    _schedule_pending()
    updated = store.get_task(task_id)
    assert updated is not None
    return _to_status_record(updated)


@router.post("/tasks/{task_id}/move", response_model=list[TaskStatus])
def move_task(task_id: str, payload: MoveTaskRequest) -> list[TaskStatus]:
    try:
        _get_store().move_before(task_id, payload.before_task_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"任务不存在：{exc}")
    _schedule_pending()
    return list_tasks()


# 兼容已有单元测试和内部调用的纯转换帮助函数。
def _completion_status(result) -> str:
    return "done" if result.quality_report.status == QualityStatus.PASS else "quality_failed"


def _to_out(result) -> DistillResultOut:
    kept = (
        round(result.total_output_chars / result.total_source_chars, 4)
        if result.total_source_chars
        else 0.0
    )
    return DistillResultOut(
        book_title=result.book_title,
        book_type=result.book_type,
        strength=result.strength,
        final_text=result.final_text,
        chapters=[
            ChapterDistillOut(
                title=chapter.title,
                source_chars=chapter.source_chars,
                target_chars=chapter.target_chars,
                output_chars=chapter.output_chars,
                error=chapter.error,
                unit_id=chapter.unit_id,
            )
            for chapter in result.chapters
        ],
        total_source_chars=result.total_source_chars,
        total_output_chars=result.total_output_chars,
        api_calls=result.api_calls,
        cache_hits=result.cache_hits,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        prompt_cache_hit_tokens=result.prompt_cache_hit_tokens,
        prompt_cache_miss_tokens=result.prompt_cache_miss_tokens,
        actual_cost_cny=result.actual_cost_cny,
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
    persist_distill_result(book_id, result)
