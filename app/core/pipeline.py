"""拆解流水线（占位）。

流水线阶段：extract（提取）→ prune（删减）→ distill（压缩提炼）→ export（导出）。
各阶段函数在对应里程碑实现，此处仅定义契约与阶段常量。
"""
from __future__ import annotations

from dataclasses import dataclass


class Stage(str):
    """流水线阶段名。"""

    EXTRACT = "extract"
    PRUNE = "prune"
    DISTILL = "distill"
    EXPORT = "export"


@dataclass
class BookDraft:
    """一本书的拆解中间产物。"""

    book_id: str
    title: str
    source_format: str
    raw_text: str = ""
    pruned_text: str = ""
    distilled_text: str = ""
    output_path: str = ""


def run_pipeline(book_id: str) -> BookDraft:
    """执行完整拆解流水线（M3/M4 实现）。"""
    raise NotImplementedError("拆解流水线将在 M3/M4 里程碑实现")
