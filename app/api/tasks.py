"""持久化的一键拆解任务 API。"""
from __future__ import annotations

import asyncio
import json
import re
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi import APIRouter, HTTPException

from .. import config
from ..core.distiller import (
    DEFAULT_STRENGTH,
    MAX_CHUNK_CHARS,
    STRENGTH_RATIOS,
    TYPE_FACTORS,
    DistillCancelled,
    DistillInterrupted,
    count_distill_calls,
)
from ..core.estimation import estimate_request
from ..core.pipeline import run_pipeline
from ..core.task_store import BookDeletionInProgressError, StoredTask, TaskStore
from ..models.schemas import (
    ChapterDistillOut,
    DeletionResult,
    DisassembleRequest,
    DistillResultOut,
    RevealOutputResult,
    TaskStatus,
)
from ..core.extractors.base import Chapter

router = APIRouter(prefix="/api", tags=["tasks"])

_lock = threading.Lock()
_active: set[str] = set()
_store: TaskStore | None = None
_store_path: Path | None = None
_executor: ThreadPoolExecutor | None = None
_shutdown_requested = threading.Event()
TASK_ID_RE = re.compile(r"^task_[A-Za-z0-9_-]{1,127}$")


def _validate_task_id(task_id: str) -> str:
    if not TASK_ID_RE.fullmatch(task_id):
        raise HTTPException(status_code=400, detail="非法任务 ID")
    return task_id


def _get_store() -> TaskStore:
    global _store, _store_path
    path = Path(config.TASK_DB)
    with _lock:
        if _store is None or _store_path != path:
            _store = TaskStore(path)
            _store_path = path
        return _store


def _get_executor() -> ThreadPoolExecutor:
    global _executor
    with _lock:
        if _executor is None:
            _executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="book-pipeline")
        return _executor


def reset_runtime_for_tests() -> None:
    global _store, _store_path, _executor
    _shutdown_requested.set()
    with _lock:
        executor = _executor
        _executor = None
        _store = None
        _store_path = None
        _active.clear()
    if executor:
        executor.shutdown(wait=True, cancel_futures=True)
    _shutdown_requested.clear()


def _estimate(book_id: str, book_type: str, strength: str) -> dict:
    path = config.BOOKS_DIR / f"{book_id}.txt"
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    try:
        meta = json.loads(
            (config.BOOKS_DIR / f"{book_id}.meta.json").read_text(encoding="utf-8")
        )
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        meta = {}
    chapters = [
        Chapter(
            item.get("title", ""),
            item.get("level", 1),
            item.get("start_char", 0),
            item.get("end_char", len(text)),
        )
        for item in meta.get("chapters", [])
    ]
    ratio = STRENGTH_RATIOS.get(strength, STRENGTH_RATIOS[DEFAULT_STRENGTH])
    ratio *= TYPE_FACTORS.get(book_type, 1.0)
    return estimate_request(
        text,
        target_ratio=ratio,
        max_chunk_chars=MAX_CHUNK_CHARS,
        api_calls=count_distill_calls(text, chapters),
    )


def _status(task: StoredTask) -> TaskStatus:
    status = "error" if task.status == "quality_failed" else task.status
    message = (
        "旧版本任务未完成，可恢复或重新发起"
        if task.status == "quality_failed"
        else task.message
    )
    return TaskStatus(
        task_id=task.task_id,
        book_id=task.book_id,
        status=status,
        stage=task.stage,
        current=task.current,
        total=task.total,
        error=task.error,
        message=message,
        delete_requested=task.delete_requested,
        estimate=task.estimate,
        result=task.result,
    )


def _run(task_id: str) -> None:
    store = _get_store()
    task = store.get_task(task_id)
    if task is None or not store.claim_task(task_id):
        with _lock:
            _active.discard(task_id)
        _schedule_pending()
        return
    try:
        asyncio.run(
            run_pipeline(
                task.book_id,
                book_type=task.book_type,
                strength=task.strength,
                task_store=store,
                task_id=task.task_id,
                should_interrupt=_shutdown_requested.is_set,
            )
        )
    except (DistillCancelled, DistillInterrupted):
        pass
    except Exception as exc:
        current = store.get_task(task_id)
        if (
            current
            and not current.delete_requested
            and current.status in {"pending", "running"}
        ):
            store.update_task(
                task_id,
                status="error",
                stage="error",
                error=str(exc),
                message="拆解失败",
            )
    finally:
        store.finalize_task_delete(task_id)
        with _lock:
            _active.discard(task_id)
        _schedule_pending()


def _schedule(task_id: str) -> bool:
    if _shutdown_requested.is_set():
        return False
    with _lock:
        if task_id in _active:
            return True
        if _active:
            return False
        _active.add(task_id)
    _get_executor().submit(_run, task_id)
    return True


def _schedule_pending() -> None:
    if _shutdown_requested.is_set():
        return
    for task in _get_store().list_tasks():
        if (
            task.status == "pending"
            and not task.cancel_requested
            and not task.delete_requested
        ):
            if not _schedule(task.task_id):
                return


def recover_and_schedule() -> int:
    _shutdown_requested.clear()
    recovered = _get_store().recover_interrupted()
    _schedule_pending()
    return recovered


def shutdown_runtime() -> None:
    global _executor
    _shutdown_requested.set()
    with _lock:
        executor = _executor
        _executor = None
    if executor:
        # 等待当前 HTTP 请求返回并持久化该单元；蒸馏器
        # 会在下一单元前停止，确保旧进程真正退出，
        # 不与新服务的 worker 同时写入数据库。
        executor.shutdown(wait=True, cancel_futures=True)


@router.get("/books/{book_id}/estimate")
def estimate_disassemble(
    book_id: str,
    book_type: str = "general",
    strength: str = "standard",
) -> dict:
    if not (config.BOOKS_DIR / f"{book_id}.txt").exists():
        raise HTTPException(status_code=404, detail="未找到书籍文本")
    if book_type not in TYPE_FACTORS or strength not in STRENGTH_RATIOS:
        raise HTTPException(status_code=400, detail="未知书籍类型或压缩强度")
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
    if not config.has_cloud_consent():
        if not req.cloud_consent:
            raise HTTPException(
                status_code=400,
                detail="首次调用前需确认：必要正文片段会发送给 DeepSeek",
            )
        config.confirm_cloud_consent()
    if not config.get_api_key():
        raise HTTPException(status_code=400, detail="尚未配置 DeepSeek API Key，请先填写")

    store = _get_store()
    if any(
        task.book_id == book_id and task.status in {"pending", "running"}
        for task in store.list_tasks()
    ):
        raise HTTPException(status_code=409, detail="该书已有等待中或运行中的任务")
    try:
        task = store.create_task(
            book_id,
            req.book_type,
            req.strength,
            estimate=_estimate(book_id, req.book_type, req.strength),
        )
    except BookDeletionInProgressError:
        raise HTTPException(status_code=409, detail="书籍正在删除，不能创建新任务")
    _schedule_pending()
    return _status(task)


@router.get("/tasks", response_model=list[TaskStatus])
def list_tasks() -> list[TaskStatus]:
    return [_status(task) for task in _get_store().list_tasks()]


@router.get("/tasks/{task_id}", response_model=TaskStatus)
def task_status(task_id: str) -> TaskStatus:
    task = _get_store().get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return _status(task)


@router.get("/tasks/{task_id}/result", response_model=DistillResultOut)
def task_result(task_id: str) -> DistillResultOut:
    task = _get_store().get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    if task.status != "done":
        raise HTTPException(status_code=409, detail=task.error or "任务尚未完成")
    payload = task.result
    source = int(payload.get("total_source_chars", 0))
    output = int(payload.get("total_output_chars", 0))
    return DistillResultOut(
        book_title=payload.get("book_title", task.book_id),
        book_type=payload.get("book_type", task.book_type),
        strength=payload.get("strength", task.strength),
        final_text=payload.get("final_text", ""),
        chapters=[ChapterDistillOut(**item) for item in payload.get("chapters", [])],
        total_source_chars=source,
        total_output_chars=output,
        api_calls=int(payload.get("api_calls", 0)),
        cache_hits=int(payload.get("cache_hits", 0)),
        errors=payload.get("errors", []),
        kept_ratio=round(output / source, 4) if source else 0.0,
        modality_warnings=payload.get("modality_warnings", []),
    )


@router.post("/tasks/{task_id}/cancel", response_model=TaskStatus)
def cancel_task(task_id: str) -> TaskStatus:
    store = _get_store()
    task = store.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    if task.status in {"done", "error", "cancelled"}:
        raise HTTPException(status_code=409, detail="任务已结束")
    if task.delete_requested:
        raise HTTPException(status_code=409, detail="任务正在停止并删除")
    store.request_cancel(task_id)
    return _status(store.get_task(task_id))


@router.post("/tasks/{task_id}/resume", response_model=TaskStatus)
def resume_task(task_id: str) -> TaskStatus:
    store = _get_store()
    task = store.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    if task.status not in {"cancelled", "error", "quality_failed", "pending"}:
        raise HTTPException(status_code=409, detail="当前状态不能恢复")
    if task.delete_requested:
        raise HTTPException(status_code=409, detail="任务正在停止并删除")
    store.update_task(
        task_id,
        status="pending",
        stage="resume",
        error="",
        message="等待从检查点恢复",
        cancel_requested=False,
    )
    _schedule_pending()
    return _status(store.get_task(task_id))


@router.post("/tasks/{task_id}/retry-failed", response_model=TaskStatus)
def retry_failed(task_id: str) -> TaskStatus:
    store = _get_store()
    task = store.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    if task.delete_requested:
        raise HTTPException(status_code=409, detail="任务正在停止并删除")
    count = store.reset_failed_units(task_id)
    if count == 0:
        raise HTTPException(status_code=409, detail="没有失败单元可重试")
    store.update_task(
        task_id,
        status="pending",
        stage="resume",
        error="",
        message=f"等待重试 {count} 个失败单元",
        cancel_requested=False,
    )
    _schedule_pending()
    return _status(store.get_task(task_id))


@router.delete("/tasks/{task_id}", response_model=DeletionResult)
def delete_task(task_id: str):
    _validate_task_id(task_id)
    state = _get_store().request_task_delete(task_id)
    if state == "deleting":
        from fastapi.responses import JSONResponse

        payload = DeletionResult(
            resource_id=task_id,
            state="deleting",
            message="正在停止并删除；当前不可中断请求结束后任务将消失",
        )
        return JSONResponse(status_code=202, content=payload.model_dump())
    return DeletionResult(
        resource_id=task_id,
        state="deleted",
        message="任务已删除" if state == "deleted" else "任务已不存在",
        already_absent=state == "absent",
    )


@router.post(
    "/tasks/{task_id}/reveal-output",
    response_model=RevealOutputResult,
)
def reveal_task_output(task_id: str) -> RevealOutputResult:
    _validate_task_id(task_id)
    task = _get_store().get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    if task.status != "done" or task.delete_requested:
        raise HTTPException(status_code=409, detail="只有已完成任务可以打开文件夹")
    raw_path = str(task.result.get("output_path") or "").strip()
    if not raw_path:
        raise HTTPException(status_code=410, detail="导出文件不存在或无法访问")
    try:
        output_path = Path(raw_path).expanduser().resolve(strict=True)
    except (FileNotFoundError, OSError):
        raise HTTPException(status_code=410, detail="导出文件不存在或无法访问")
    if not output_path.is_file():
        raise HTTPException(status_code=410, detail="导出文件不存在或无法访问")
    if sys.platform != "darwin":
        raise HTTPException(status_code=501, detail="当前系统不支持 Finder 定位")
    try:
        subprocess.run(
            ["open", "-R", str(output_path)],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
            shell=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise HTTPException(status_code=502, detail=f"Finder 打开失败：{exc}")
    return RevealOutputResult(ok=True, path=str(output_path), message="已在 Finder 中定位")
