"""提炼单元的取消检查、幂等检查点与内容缓存。"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

from ..models.domain import DistillUnit, KnowledgeUnit
from .task_store import StoredUnit, TaskStore


class DistillCancelled(RuntimeError):
    pass


@dataclass
class TaskExecutionContext:
    store: TaskStore
    task_id: str
    source_fingerprint: str
    prune_config_hash: str
    book_type: str
    strength: str
    model: str
    prompt_version: str
    cache_hits: int = 0
    cache_misses: int = 0
    _units: dict[str, StoredUnit] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        self._refresh_units()

    def _refresh_units(self) -> None:
        self._units = {unit.unit_id: unit for unit in self.store.get_units(self.task_id)}

    def cache_key(self, unit: DistillUnit) -> str:
        payload = {
            "source_fingerprint": self.source_fingerprint,
            "prune_config_hash": self.prune_config_hash,
            "book_type": self.book_type,
            "strength": self.strength,
            "input_start": unit.target_start,
            "input_end": unit.target_end,
            "input_fingerprint": unit.input_fingerprint,
            "target_chars": unit.target_chars,
            "model": self.model,
            "prompt_version": self.prompt_version,
        }
        encoded = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def raise_if_cancelled(self) -> None:
        if self.store.is_cancel_requested(self.task_id):
            raise DistillCancelled("任务已取消，停止创建新的模型请求")

    def load_cached(self, unit: DistillUnit) -> list[KnowledgeUnit] | None:
        key = self.cache_key(unit)
        checkpoint = self._units.get(unit.unit_id)
        if (
            checkpoint
            and checkpoint.status == "done"
            and checkpoint.cache_key == key
            and checkpoint.output.get("knowledge_units")
        ):
            self.cache_hits += 1
            return [
                KnowledgeUnit.model_validate(item)
                for item in checkpoint.output["knowledge_units"]
            ]

        cached = self.store.get_cache(key)
        if cached and cached.get("knowledge_units"):
            self.cache_hits += 1
            self.store.upsert_unit(
                self.task_id,
                unit.unit_id,
                "done",
                key,
                attempts=0,
                output=cached,
            )
            self._refresh_units()
            return [
                KnowledgeUnit.model_validate(item) for item in cached["knowledge_units"]
            ]
        self.cache_misses += 1
        return None

    def start_unit(self, unit: DistillUnit) -> None:
        self.raise_if_cancelled()
        previous = self._units.get(unit.unit_id)
        attempts = (previous.attempts if previous else 0) + 1
        self.store.upsert_unit(
            self.task_id,
            unit.unit_id,
            "running",
            self.cache_key(unit),
            attempts=attempts,
        )
        self._refresh_units()

    def complete_unit(
        self, unit: DistillUnit, knowledge_units: list[KnowledgeUnit]
    ) -> None:
        self.raise_if_cancelled()
        checkpoint = self._units.get(unit.unit_id)
        attempts = checkpoint.attempts if checkpoint else 1
        output = {
            "knowledge_units": [
                item.model_dump(mode="json") for item in knowledge_units
            ]
        }
        key = self.cache_key(unit)
        self.store.upsert_unit(
            self.task_id,
            unit.unit_id,
            "done",
            key,
            attempts=attempts,
            output=output,
        )
        self.store.put_cache(key, output)
        self._refresh_units()

    def fail_unit(self, unit: DistillUnit, error: str) -> None:
        checkpoint = self._units.get(unit.unit_id)
        attempts = checkpoint.attempts if checkpoint else 1
        self.store.upsert_unit(
            self.task_id,
            unit.unit_id,
            "error",
            self.cache_key(unit),
            attempts=attempts,
            error=error,
        )
        self._refresh_units()

