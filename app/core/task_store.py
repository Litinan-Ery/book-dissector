"""SQLite 持久化任务、单元检查点、队列顺序与内容缓存。"""
from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _decode(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


@dataclass
class StoredTask:
    task_id: str
    book_id: str
    book_type: str
    strength: str
    status: str
    stage: str
    current: int
    total: int
    error: str
    message: str
    cancel_requested: bool
    queue_order: int
    created_at: str
    updated_at: str
    run_id: str = ""
    result: dict[str, Any] = field(default_factory=dict)
    estimate: dict[str, Any] = field(default_factory=dict)
    metrics: dict[str, Any] = field(default_factory=dict)


@dataclass
class StoredUnit:
    task_id: str
    unit_id: str
    status: str
    cache_key: str
    attempts: int
    error: str
    output: dict[str, Any] = field(default_factory=dict)
    updated_at: str = ""


class TaskStore:
    _TASK_COLUMNS = {
        "status",
        "stage",
        "current",
        "total",
        "error",
        "message",
        "cancel_requested",
        "run_id",
        "result_json",
        "estimate_json",
        "metrics_json",
    }

    def __init__(self, database: Path) -> None:
        self.database = database
        self.database.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 10000")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    task_id TEXT PRIMARY KEY,
                    book_id TEXT NOT NULL,
                    book_type TEXT NOT NULL,
                    strength TEXT NOT NULL,
                    status TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    current INTEGER NOT NULL DEFAULT 0,
                    total INTEGER NOT NULL DEFAULT 0,
                    error TEXT NOT NULL DEFAULT '',
                    message TEXT NOT NULL DEFAULT '',
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    queue_order INTEGER NOT NULL,
                    run_id TEXT NOT NULL DEFAULT '',
                    result_json TEXT NOT NULL DEFAULT '{}',
                    estimate_json TEXT NOT NULL DEFAULT '{}',
                    metrics_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_tasks_queue
                    ON tasks(queue_order, created_at);
                CREATE TABLE IF NOT EXISTS task_units (
                    task_id TEXT NOT NULL,
                    unit_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    cache_key TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    error TEXT NOT NULL DEFAULT '',
                    output_json TEXT NOT NULL DEFAULT '{}',
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(task_id, unit_id),
                    FOREIGN KEY(task_id) REFERENCES tasks(task_id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS unit_cache (
                    cache_key TEXT PRIMARY KEY,
                    output_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    last_hit_at TEXT NOT NULL,
                    hit_count INTEGER NOT NULL DEFAULT 0
                );
                """
            )

    def create_task(
        self,
        book_id: str,
        book_type: str,
        strength: str,
        *,
        task_id: str | None = None,
        estimate: dict[str, Any] | None = None,
    ) -> StoredTask:
        created = _now()
        task_id = task_id or "task_" + uuid.uuid4().hex[:16]
        with self._connect() as connection:
            # 先取得写锁，保证并发入队不会读到同一个 MAX(queue_order)。
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT COALESCE(MAX(queue_order), -1) + 1 AS next_order FROM tasks"
            ).fetchone()
            queue_order = int(row["next_order"])
            connection.execute(
                """
                INSERT INTO tasks (
                    task_id, book_id, book_type, strength, status, stage,
                    queue_order, estimate_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'pending', 'queued', ?, ?, ?, ?)
                """,
                (
                    task_id,
                    book_id,
                    book_type,
                    strength,
                    queue_order,
                    json.dumps(estimate or {}, ensure_ascii=False),
                    created,
                    created,
                ),
            )
        task = self.get_task(task_id)
        assert task is not None
        return task

    def get_task(self, task_id: str) -> StoredTask | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
        return self._task_from_row(row) if row else None

    def list_tasks(self) -> list[StoredTask]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM tasks ORDER BY queue_order, created_at"
            ).fetchall()
        return [self._task_from_row(row) for row in rows]

    def update_task(self, task_id: str, **changes: Any) -> None:
        if not changes:
            return
        encoded: dict[str, Any] = {}
        aliases = {"result": "result_json", "estimate": "estimate_json", "metrics": "metrics_json"}
        for key, value in changes.items():
            column = aliases.get(key, key)
            if column not in self._TASK_COLUMNS:
                raise ValueError(f"unsupported task field: {key}")
            if column.endswith("_json"):
                value = json.dumps(value or {}, ensure_ascii=False)
            if column == "cancel_requested":
                value = int(bool(value))
            encoded[column] = value
        encoded["updated_at"] = _now()
        assignments = ", ".join(f"{column} = ?" for column in encoded)
        values = [*encoded.values(), task_id]
        with self._connect() as connection:
            cursor = connection.execute(
                f"UPDATE tasks SET {assignments} WHERE task_id = ?", values
            )
            if cursor.rowcount == 0:
                raise KeyError(task_id)

    def request_cancel(self, task_id: str) -> None:
        now = _now()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE tasks
                SET cancel_requested = 1,
                    status = CASE
                        WHEN status = 'pending' THEN 'cancelled'
                        ELSE status
                    END,
                    stage = CASE
                        WHEN status = 'pending' THEN 'cancelled'
                        ELSE stage
                    END,
                    message = CASE
                        WHEN status = 'pending' THEN '任务已取消，未开始处理'
                        ELSE '取消请求已记录'
                    END,
                    updated_at = ?
                WHERE task_id = ? AND status IN ('pending', 'running')
                """,
                (now, task_id),
            )
            if cursor.rowcount == 0:
                exists = connection.execute(
                    "SELECT 1 FROM tasks WHERE task_id = ?", (task_id,)
                ).fetchone()
                if not exists:
                    raise KeyError(task_id)

    def claim_task(self, task_id: str) -> bool:
        """由单个工作线程原子领取仍可运行的等待任务。"""
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE tasks
                SET status = 'running', message = '任务开始执行', updated_at = ?
                WHERE task_id = ? AND status = 'pending' AND cancel_requested = 0
                """,
                (_now(), task_id),
            )
            return cursor.rowcount == 1

    def is_cancel_requested(self, task_id: str) -> bool:
        task = self.get_task(task_id)
        return bool(task and task.cancel_requested)

    def recover_interrupted(self) -> int:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cancelled = connection.execute(
                """
                UPDATE tasks
                SET status = 'cancelled', stage = 'cancelled',
                    message = '服务重启前已请求取消，任务已取消', updated_at = ?
                WHERE status = 'running' AND cancel_requested = 1
                """,
                (now,),
            ).rowcount
            resumable = connection.execute(
                """
                UPDATE tasks
                SET status = 'pending', stage = 'resume',
                    message = '服务重启，等待从检查点恢复', updated_at = ?
                WHERE status = 'running' AND cancel_requested = 0
                """,
                (now,),
            ).rowcount
            return cancelled + resumable

    def upsert_unit(
        self,
        task_id: str,
        unit_id: str,
        status: str,
        cache_key: str,
        *,
        attempts: int = 0,
        error: str = "",
        output: dict[str, Any] | None = None,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO task_units (
                    task_id, unit_id, status, cache_key, attempts, error,
                    output_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(task_id, unit_id) DO UPDATE SET
                    status = excluded.status,
                    cache_key = excluded.cache_key,
                    attempts = excluded.attempts,
                    error = excluded.error,
                    output_json = excluded.output_json,
                    updated_at = excluded.updated_at
                """,
                (
                    task_id,
                    unit_id,
                    status,
                    cache_key,
                    attempts,
                    error,
                    json.dumps(output or {}, ensure_ascii=False),
                    _now(),
                ),
            )

    def get_units(self, task_id: str) -> list[StoredUnit]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM task_units WHERE task_id = ? ORDER BY rowid", (task_id,)
            ).fetchall()
        return [
            StoredUnit(
                task_id=row["task_id"],
                unit_id=row["unit_id"],
                status=row["status"],
                cache_key=row["cache_key"],
                attempts=row["attempts"],
                error=row["error"],
                output=_decode(row["output_json"], {}),
                updated_at=row["updated_at"],
            )
            for row in rows
        ]

    def reset_failed_units(self, task_id: str) -> int:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE task_units
                SET status = 'pending', error = '', output_json = '{}', updated_at = ?
                WHERE task_id = ? AND status = 'error'
                """,
                (_now(), task_id),
            )
            return cursor.rowcount

    def put_cache(self, cache_key: str, output: dict[str, Any]) -> None:
        now = _now()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO unit_cache (
                    cache_key, output_json, created_at, last_hit_at, hit_count
                ) VALUES (?, ?, ?, ?, 0)
                ON CONFLICT(cache_key) DO UPDATE SET
                    output_json = excluded.output_json,
                    last_hit_at = excluded.last_hit_at
                """,
                (cache_key, json.dumps(output, ensure_ascii=False), now, now),
            )

    def get_cache(self, cache_key: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT output_json FROM unit_cache WHERE cache_key = ?", (cache_key,)
            ).fetchone()
            if not row:
                return None
            connection.execute(
                """
                UPDATE unit_cache
                SET hit_count = hit_count + 1, last_hit_at = ?
                WHERE cache_key = ?
                """,
                (_now(), cache_key),
            )
        return _decode(row["output_json"], {})

    def move_before(self, task_id: str, before_task_id: str) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT task_id FROM tasks ORDER BY queue_order, created_at"
            ).fetchall()
            ids = [row["task_id"] for row in rows]
            if task_id not in ids or before_task_id not in ids:
                raise KeyError(task_id if task_id not in ids else before_task_id)
            if task_id == before_task_id:
                return
            ids.remove(task_id)
            ids.insert(ids.index(before_task_id), task_id)
            updated = _now()
            connection.executemany(
                "UPDATE tasks SET queue_order = ?, updated_at = ? WHERE task_id = ?",
                [(index, updated, current_id) for index, current_id in enumerate(ids)],
            )

    @staticmethod
    def _task_from_row(row: sqlite3.Row) -> StoredTask:
        return StoredTask(
            task_id=row["task_id"],
            book_id=row["book_id"],
            book_type=row["book_type"],
            strength=row["strength"],
            status=row["status"],
            stage=row["stage"],
            current=row["current"],
            total=row["total"],
            error=row["error"],
            message=row["message"],
            cancel_requested=bool(row["cancel_requested"]),
            queue_order=row["queue_order"],
            run_id=row["run_id"],
            result=_decode(row["result_json"], {}),
            estimate=_decode(row["estimate_json"], {}),
            metrics=_decode(row["metrics_json"], {}),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
