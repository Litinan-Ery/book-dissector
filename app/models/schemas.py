"""API 数据模型（Pydantic schemas）。"""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class BookInfo(BaseModel):
    """导入书籍的基本信息（含 M2 文本提取结果）。"""

    id: str
    filename: str
    size_bytes: int
    uploaded_at: datetime
    title: str = ""
    author: str = ""
    source_format: str = ""
    word_count: int = 0
    extract_status: str = "pending"  # pending / processing / ok / error
    extract_error: str = ""


class SettingsUpdate(BaseModel):
    """设置项更新请求。"""

    deepseek_api_key: str = Field(default="", max_length=512)


class SettingsView(BaseModel):
    """设置项视图（密钥不回显完整值）。"""

    deepseek_api_key_configured: bool
    deepseek_model: str


class ApiKeyTestResult(BaseModel):
    """测试 DeepSeek 连接的结果。"""

    ok: bool
    message: str


class TaskStatus(BaseModel):
    """拆解任务状态。"""

    task_id: str
    status: str  # pending / running / done / error
    stage: str = ""
    current: int = 0
    total: int = 0
    error: str = ""
    message: str = ""


class DisassembleRequest(BaseModel):
    """发起拆解请求。"""

    book_type: str = "general"   # general / fiction / technical
    strength: str = "standard"   # conservative / standard / aggressive


class ChapterDistillOut(BaseModel):
    """一章的蒸馏结果。"""

    title: str
    source_chars: int
    target_chars: int
    output_chars: int
    error: str = ""


class DistillResultOut(BaseModel):
    """全书蒸馏结果。"""

    book_title: str
    book_type: str
    strength: str
    final_text: str
    chapters: list[ChapterDistillOut]
    total_source_chars: int
    total_output_chars: int
    api_calls: int
    errors: list[str]
    kept_ratio: float


class PruneRegion(BaseModel):
    """一个删除区域。"""

    start: int
    end: int
    reason: str  # copyright / toc / backmatter / duplicate
    label: str


class ChapterInfo(BaseModel):
    """章节信息（删减稿中的新偏移）。"""

    title: str
    level: int
    start_char: int
    end_char: int


class PruneResultOut(BaseModel):
    """删减结果。"""

    pruned_text: str
    regions: list[PruneRegion]
    original_chars: int
    removed_chars: int
    kept_ratio: float
    pruned_chapters: list[ChapterInfo] = []


class RestoreRequest(BaseModel):
    """恢复请求：需要恢复保留的原始区间列表。"""

    regions: list[list[int]]  # [[start, end], ...]


class ExportResultOut(BaseModel):
    """导出结果。"""

    filename: str
    path: str
    size_bytes: int


class ExportInfo(BaseModel):
    """已导出的文件信息。"""

    filename: str
    size_bytes: int
    created_at: float
