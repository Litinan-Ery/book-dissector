"""SQLite 持久化任务、单元检查点、队列顺序与内容缓存。"""
from __future__ import annotations

import json
import sqlite3
import threading
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
    delete_requested: bool
    queue_order: int
    created_at: str
    updated_at: str
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


@dataclass
class BookDeletionRecord:
    book_id: str
    state: str
    recycle_path: str
    manifest: list[dict[str, str]] = field(default_factory=list)
    error: str = ""
    created_at: str = ""
    updated_at: str = ""


class ActiveBookTasksError(RuntimeError):
    def __init__(self, task_ids: list[str]) -> None:
        self.task_ids = task_ids
        super().__init__("书籍仍有关联的等待中或运行中任务")


class BookDeletionInProgressError(RuntimeError):
    pass


class TaskStore:
    _INIT_LOCK = threading.Lock()
    _TASK_COLUMNS = {
        "status",
        "stage",
        "current",
        "total",
        "error",
        "message",
        "cancel_requested",
        "delete_requested",
        "result_json",
        "estimate_json",
        "metrics_json",
    }

    def __init__(self, database: Path) -> None:
        self.database = database
        self.database.parent.mkdir(parents=True, exist_ok=True)
        with self._INIT_LOCK:
            self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 10000")
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
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
                    delete_requested INTEGER NOT NULL DEFAULT 0,
                    queue_order INTEGER NOT NULL,
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
                    book_id TEXT NOT NULL DEFAULT '',
                    output_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    last_hit_at TEXT NOT NULL,
                    hit_count INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS book_deletions (
                    book_id TEXT PRIMARY KEY,
                    state TEXT NOT NULL,
                    recycle_path TEXT NOT NULL DEFAULT '',
                    manifest_json TEXT NOT NULL DEFAULT '[]',
                    error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )
            self._ensure_column(
                connection,
                "tasks",
                "delete_requested",
                "INTEGER NOT NULL DEFAULT 0",
            )
            self._ensure_column(
                connection,
                "unit_cache",
                "book_id",
                "TEXT NOT NULL DEFAULT ''",
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_unit_cache_book ON unit_cache(book_id)"
            )
            connection.execute(
                """
                UPDATE unit_cache
                SET book_id = COALESCE((
                    SELECT tasks.book_id
                    FROM task_units
                    JOIN tasks ON tasks.task_id = task_units.task_id
                    WHERE task_units.cache_key = unit_cache.cache_key
                    LIMIT 1
                ), '')
                WHERE book_id = ''
                """
            )

    @staticmethod
    def _ensure_column(
        connection: sqlite3.Connection,
        table: str,
        column: str,
        definition: str,
    ) -> None:
        columns = {
            row["name"]
            for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if column not in columns:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

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
            connection.execute("BEGIN IMMEDIATE")
            deleting = connection.execute(
                "SELECT state FROM book_deletions WHERE book_id = ?",
                (book_id,),
            ).fetchone()
            if deleting is not None:
                raise BookDeletionInProgressError(book_id)
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
            if column in {"cancel_requested", "delete_requested"}:
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
        task = self.get_task(task_id)
        if task is None:
            raise KeyError(task_id)
        if task.status == "pending":
            self.update_task(
                task_id,
                status="cancelled",
                stage="cancelled",
                cancel_requested=True,
                message="等待中的任务已取消",
            )
        else:
            self.update_task(task_id, cancel_requested=True, message="取消请求已记录")

    def claim_task(self, task_id: str) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE tasks
                SET status = 'running', stage = 'prune', message = '任务开始执行',
                    updated_at = ?
                WHERE task_id = ? AND status = 'pending'
                    AND cancel_requested = 0 AND delete_requested = 0
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
            deleted = connection.execute(
                "DELETE FROM tasks WHERE delete_requested = 1"
            ).rowcount
            cancelled = connection.execute(
                """
                UPDATE tasks
                SET status = 'cancelled', stage = 'cancelled',
                    message = '服务重启前已请求取消，任务已取消', updated_at = ?
                WHERE status = 'running' AND cancel_requested = 1
                    AND delete_requested = 0
                """,
                (now,),
            ).rowcount
            resumable = connection.execute(
                """
                UPDATE tasks
                SET status = 'pending', stage = 'resume',
                    message = '服务重启，等待从检查点恢复', updated_at = ?
                WHERE status = 'running' AND cancel_requested = 0
                    AND delete_requested = 0
                """,
                (now,),
            ).rowcount
            return deleted + cancelled + resumable

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

    def put_cache(
        self,
        cache_key: str,
        output: dict[str, Any],
        *,
        book_id: str = "",
    ) -> None:
        now = _now()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO unit_cache (
                    cache_key, book_id, output_json, created_at, last_hit_at, hit_count
                ) VALUES (?, ?, ?, ?, ?, 0)
                ON CONFLICT(cache_key) DO UPDATE SET
                    book_id = CASE
                        WHEN excluded.book_id != '' THEN excluded.book_id
                        ELSE unit_cache.book_id
                    END,
                    output_json = excluded.output_json,
                    last_hit_at = excluded.last_hit_at
                """,
                (
                    cache_key,
                    book_id,
                    json.dumps(output, ensure_ascii=False),
                    now,
                    now,
                ),
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

    def request_task_delete(self, task_id: str) -> str:
        """请求删除任务，返回 deleted/deleting/absent。"""
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status, delete_requested FROM tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            if row is None:
                return "absent"
            if row["status"] == "running":
                connection.execute(
                    """
                    UPDATE tasks
                    SET delete_requested = 1, cancel_requested = 1,
                        stage = 'deleting', message = '正在停止并删除',
                        updated_at = ?
                    WHERE task_id = ?
                    """,
                    (now, task_id),
                )
                return "deleting"
            connection.execute("DELETE FROM tasks WHERE task_id = ?", (task_id,))
            return "deleted"

    def finalize_task_delete(self, task_id: str) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM tasks WHERE task_id = ? AND delete_requested = 1",
                (task_id,),
            )
            return cursor.rowcount == 1

    def is_delete_requested(self, task_id: str) -> bool:
        task = self.get_task(task_id)
        return bool(task and task.delete_requested)

    def list_tasks_for_book(self, book_id: str) -> list[StoredTask]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM tasks
                WHERE book_id = ?
                ORDER BY queue_order, created_at
                """,
                (book_id,),
            ).fetchall()
        return [self._task_from_row(row) for row in rows]

    def prepare_book_deletion(
        self,
        book_id: str,
        recycle_path: str,
        manifest: list[dict[str, str]],
    ) -> str:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT state FROM book_deletions WHERE book_id = ?",
                (book_id,),
            ).fetchone()
            if existing is not None:
                return str(existing["state"])
            active_rows = connection.execute(
                """
                SELECT task_id FROM tasks
                WHERE book_id = ? AND status IN ('pending', 'running')
                ORDER BY queue_order, created_at
                """,
                (book_id,),
            ).fetchall()
            active = [str(row["task_id"]) for row in active_rows]
            if active:
                raise ActiveBookTasksError(active)
            connection.execute(
                """
                INSERT INTO book_deletions (
                    book_id, state, recycle_path, manifest_json, error,
                    created_at, updated_at
                ) VALUES (?, 'preparing', ?, ?, '', ?, ?)
                """,
                (
                    book_id,
                    recycle_path,
                    json.dumps(manifest, ensure_ascii=False),
                    now,
                    now,
                ),
            )
        return "preparing"

    def mark_book_deletion_staged(self, book_id: str) -> None:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE book_deletions
                SET state = 'staged', error = '', updated_at = ?
                WHERE book_id = ? AND state != 'completed'
                """,
                (_now(), book_id),
            )
            if cursor.rowcount == 0:
                raise KeyError(book_id)

    def fail_book_deletion(self, book_id: str, error: str) -> None:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE book_deletions
                SET state = 'failed', error = ?, updated_at = ?
                WHERE book_id = ? AND state != 'completed'
                """,
                (error, _now(), book_id),
            )
            if cursor.rowcount == 0:
                raise KeyError(book_id)

    def abort_book_deletion(self, book_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM book_deletions WHERE book_id = ? AND state != 'completed'",
                (book_id,),
            )

    def finalize_book_deletion(self, book_id: str) -> None:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            record = connection.execute(
                "SELECT state FROM book_deletions WHERE book_id = ?",
                (book_id,),
            ).fetchone()
            if record is None:
                raise KeyError(book_id)
            if record["state"] == "completed":
                return
            active_rows = connection.execute(
                """
                SELECT task_id FROM tasks
                WHERE book_id = ? AND status IN ('pending', 'running')
                ORDER BY queue_order, created_at
                """,
                (book_id,),
            ).fetchall()
            active = [str(row["task_id"]) for row in active_rows]
            if active:
                raise ActiveBookTasksError(active)
            connection.execute(
                """
                DELETE FROM unit_cache
                WHERE book_id = ? OR cache_key IN (
                    SELECT task_units.cache_key
                    FROM task_units
                    JOIN tasks ON tasks.task_id = task_units.task_id
                    WHERE tasks.book_id = ?
                )
                """,
                (book_id, book_id),
            )
            connection.execute("DELETE FROM tasks WHERE book_id = ?", (book_id,))
            connection.execute(
                """
                UPDATE book_deletions
                SET state = 'completed', error = '', updated_at = ?
                WHERE book_id = ?
                """,
                (now, book_id),
            )

    def get_book_deletion(self, book_id: str) -> BookDeletionRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM book_deletions WHERE book_id = ?",
                (book_id,),
            ).fetchone()
        return self._book_deletion_from_row(row) if row else None

    def list_incomplete_book_deletions(self) -> list[BookDeletionRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM book_deletions
                WHERE state != 'completed'
                ORDER BY created_at
                """
            ).fetchall()
        return [self._book_deletion_from_row(row) for row in rows]

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
            delete_requested=bool(row["delete_requested"]),
            queue_order=row["queue_order"],
            result=_decode(row["result_json"], {}),
            estimate=_decode(row["estimate_json"], {}),
            metrics=_decode(row["metrics_json"], {}),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _book_deletion_from_row(row: sqlite3.Row) -> BookDeletionRecord:
        manifest = _decode(row["manifest_json"], [])
        return BookDeletionRecord(
            book_id=row["book_id"],
            state=row["state"],
            recycle_path=row["recycle_path"],
            manifest=manifest if isinstance(manifest, list) else [],
            error=row["error"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
