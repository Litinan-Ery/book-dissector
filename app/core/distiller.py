"""核心内容压缩与观点提取（DeepSeek API）。

策略：分段式蒸馏（分治）——按章节分批调用大模型，每章给出明确的
"原文字数 → 目标字数"预算，保证各章比例均匀；最后按原章节顺序
合并为全书精华稿。

数据差异化（FR-3.2 / 决策点3）：
- general（社科/商业/工具书）：核心观点 + 论据 + 最关键例子（保留约 15%）
- fiction（小说）：主要人物 + 主线剧情 + 关键场景 + 结局（保留约 10%）
- technical（技术书）：概念定义 + 方法流程 + 关键参数/代码要点（保留约 20%）

忠实性（FR-3.5 / AC-6）：temperature 低 + 提示词硬性约束 + 输出字数核验。
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Callable

import httpx

from .. import config
from .extractors.base import Chapter

# 单次请求的输入上限（字符）。超出按段落重叠切片，避免超长上下文。
MAX_CHUNK_CHARS = 12000
# 切片重叠字符数，降低跨片逻辑断裂风险
OVERLAP_CHARS = 500

# 压缩强度 → 目标保留比例（FR-3.4）
STRENGTH_RATIOS = {
    "conservative": 0.25,  # 保守：保留约 25%
    "standard": 0.15,      # 标准：保留约 15%
    "aggressive": 0.08,    # 激进：保留约 8%
}
DEFAULT_STRENGTH = "standard"

# 数据类型 → 目标比例系数（相对标准强度的调整）
TYPE_FACTORS = {
    "general": 1.0,
    "fiction": 0.7,
    "technical": 1.3,
}

ProgressCb = Callable[[str, int, int], None]  # (stage_msg, current, total)


@dataclass
class ChapterDistill:
    """一章的蒸馏结果。"""

    title: str
    source_chars: int
    target_chars: int
    output_chars: int
    text: str = ""
    error: str = ""


@dataclass
class DistillResult:
    """全书蒸馏结果。"""

    book_title: str
    book_type: str
    strength: str
    chapters: list[ChapterDistill] = field(default_factory=list)
    total_source_chars: int = 0
    total_output_chars: int = 0
    api_calls: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def final_text(self) -> str:
        parts: list[str] = []
        for ch in self.chapters:
            if ch.text.strip():
                parts.append(ch.text.strip())
        return "\n\n".join(parts)


def _ratio_for(book_type: str, strength: str) -> float:
    base = STRENGTH_RATIOS.get(strength, STRENGTH_RATIOS[DEFAULT_STRENGTH])
    return base * TYPE_FACTORS.get(book_type, 1.0)


def _system_prompt(book_type: str) -> str:
    if book_type == "fiction":
        return (
            "你是资深图书拆解专家，擅长把小说压缩为\"情节精华\"。\n"
            "任务：基于给定的小说章节原文，提取：\n"
            "1. 主要人物及其定位（一句话）；\n"
            "2. 主线剧情进展（按原文顺序的关键事件链）；\n"
            "3. 关键场景（最能体现冲突/转折/情感高潮的场景，每个 2-4 句，必须来自原文）；\n"
            "4. 本章出现的伏笔/悬念（如有）。\n"
            "硬性要求：\n"
            "- 只基于给定文本，严禁添加原文没有的人物、情节、细节；\n"
            "- 不改变故事走向与人物关系；\n"
            "- 输出 Markdown：以 `## 章节标题` 开头，用 **人物**/**剧情**/**关键场景** 分节；\n"
            "- 控制在目标字数内，宁可精炼不可注水。"
        )
    if book_type == "technical":
        return (
            "你是资深图书拆解专家，擅长把技术类书籍压缩为\"方法与要点\"。\n"
            "任务：基于给定的章节原文，提取：\n"
            "1. 核心概念与定义（含关键术语，首次出现时给一句话解释）；\n"
            "2. 方法/流程/步骤（按原文逻辑顺序）；\n"
            "3. 关键参数、公式、代码要点（如原文有，尽量保留原样）；\n"
            "4. 常见误区或注意事项（如原文提到）。\n"
            "硬性要求：\n"
            "- 只基于给定文本，严禁添加原文没有的技术细节；\n"
            "- 术语与公式必须与原文一致，不得改写含义；\n"
            "- 输出 Markdown：以 `## 章节标题` 开头，用 **概念**/**方法**/**要点** 分节；\n"
            "- 控制在目标字数内，优先保留可操作内容。"
        )
    # general（默认）
    return (
        "你是资深图书拆解专家，擅长把书籍压缩为\"观点精华\"。\n"
        "任务：基于给定的章节原文，提取：\n"
        "1. 核心观点：作者的核心论点与结论（保持原意与立场）；\n"
        "2. 关键论据：支撑观点的逻辑、数据、事实（简明扼要）；\n"
        "3. 最关键、最生动的例子：本章至少保留 1 个，必须来自原文，可稍作精简；\n"
        "4. 关键术语：首次出现时给一句话解释。\n"
        "硬性要求：\n"
        "- 只基于给定文本，严禁添加原文没有的观点、数据、例子；\n"
        "- 忠实原意，不得改变作者的立场或结论；\n"
        "- 输出 Markdown：以 `## 章节标题` 开头，用 **核心观点**/**论据**/**例子** 分节；\n"
        "- 控制在目标字数内，观点优先，例子精炼。"
    )


def _user_prompt(
    book_title: str, index: int, total: int, chapter_title: str, text: str,
    target_chars: int,
) -> str:
    return (
        f"书籍：《{book_title}》\n"
        f"这是第 {index}/{total} 章「{chapter_title}」。\n"
        f"本章原文约 {len(text)} 字，压缩目标约 {target_chars} 字"
        f"（允许 ±25% 浮动）。\n"
        f"请输出本章精华 Markdown。\n\n"
        f"--- 原文开始 ---\n{text}\n--- 原文结束 ---"
    )


def _split_chapter(text: str, max_chars: int = MAX_CHUNK_CHARS) -> list[str]:
    """超长章节按段落切块，块间保留重叠。"""
    if len(text) <= max_chars:
        return [text]
    paras = re.split(r"(\n\s*\n)", text)
    chunks: list[str] = []
    cur = ""
    for p in paras:
        if len(cur) + len(p) > max_chars and cur:
            chunks.append(cur)
            cur = cur[-OVERLAP_CHARS:] + p if len(cur) > OVERLAP_CHARS else p
        else:
            cur += p
    if cur.strip():
        chunks.append(cur)
    return chunks


async def _call_deepseek(system: str, user: str, api_key: str) -> str:
    """调用 DeepSeek chat completions，返回 assistant 文本。"""
    payload = {
        "model": config.DEEPSEEK_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0.3,
        "max_tokens": 4096,
    }
    async with httpx.AsyncClient(timeout=180) as client:
        resp = await client.post(
            f"{config.DEEPSEEK_BASE_URL}/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
        )
    if resp.status_code == 401:
        raise RuntimeError("API Key 无效（401），请在设置中重新填写")
    if resp.status_code == 429:
        raise RuntimeError("请求过于频繁（429），请稍后重试")
    if resp.status_code != 200:
        raise RuntimeError(f"DeepSeek API 错误（HTTP {resp.status_code}）：{resp.text[:200]}")
    data = resp.json()
    try:
        return data["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError, TypeError):
        raise RuntimeError("DeepSeek 响应格式异常（缺少 choices/message/content）")


def _fake_call(system: str, user: str, api_key: str) -> str:
    """本地假实现（测试用，不真实调用 API）。"""
    m = re.search(r"第 (\d+)/(\d+) 章「(.+?)」", user)
    title = m.group(3) if m else "未知章节"
    return f"## {title}\n\n**核心观点**：原文观点（FAKE 测试输出，不真实调用）。\n\n**例子**：原文中的示例。"  # noqa: E501


async def distill_book(
    book_id: str,
    book_type: str = "general",
    strength: str = DEFAULT_STRENGTH,
    progress: ProgressCb | None = None,
    use_fake: bool | None = None,
) -> DistillResult:
    """对一本书执行分段蒸馏。进度回调 (stage, current, total)。"""
    if book_type not in TYPE_FACTORS:
        raise ValueError(f"未知书籍类型：{book_type}")
    if strength not in STRENGTH_RATIOS:
        raise ValueError(f"未知压缩强度：{strength}")

    api_key = config.get_api_key()
    if not api_key:
        raise RuntimeError("尚未配置 DeepSeek API Key，请先在设置中填写")
    if use_fake is None:
        use_fake = os.environ.get("BOOK_DISSECTOR_FAKE_DEEPSEEK") == "1"

    # 读取删减稿（M3 产物）；无删减稿则退回原始提取文本
    pruned_path = config.INTERMEDIATE_DIR / f"{book_id}.pruned.txt"
    text_path = config.BOOKS_DIR / f"{book_id}.txt"
    meta_path = config.BOOKS_DIR / f"{book_id}.meta.json"

    if pruned_path.exists():
        text = pruned_path.read_text(encoding="utf-8")
    elif text_path.exists():
        text = text_path.read_text(encoding="utf-8")
    else:
        raise FileNotFoundError("未找到书籍文本，请先上传并完成提取")

    meta: dict = {}
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            meta = {}
    book_title = meta.get("title") or book_id

    # 章节偏移：优先取删减稿的映射（M3 产物），否则用原始提取的偏移
    prune_meta_path = config.INTERMEDIATE_DIR / f"{book_id}.prune.json"
    chapters: list[Chapter] = []
    if prune_meta_path.exists():
        try:
            prune_meta = json.loads(prune_meta_path.read_text(encoding="utf-8"))
            chapters = [
                Chapter(c.get("title", ""), c.get("level", 1), c.get("start_char", 0), c.get("end_char", len(text)))
                for c in prune_meta.get("pruned_chapters", [])
            ]
        except json.JSONDecodeError:
            chapters = []
    if not chapters:
        chapters = [
            Chapter(c.get("title", ""), c.get("level", 1), c.get("start_char", 0), c.get("end_char", len(text)))
            for c in meta.get("chapters", [])
        ]

    # 蒸馏单元：有章节结构按章节（优先用删减稿的新偏移），无则按文本切片
    units: list[tuple[str, str]] = []  # (title, text)
    if chapters:
        for i, ch in enumerate(chapters):
            seg = text[ch.start_char : ch.end_char] if ch.end_char > ch.start_char else ""
            if seg.strip():
                units.append((ch.title or f"章节 {i+1}", seg))
    if not units:
        for i, seg in enumerate(_split_chapter(text)):
            units.append((f"第 {i+1} 部分", seg))

    ratio = _ratio_for(book_type, strength)
    result = DistillResult(
        book_title=book_title, book_type=book_type, strength=strength
    )
    system = _system_prompt(book_type)

    for i, (title, seg) in enumerate(units):
        if progress:
            progress(f"蒸馏中：{title}", i, len(units))
        chunks = _split_chapter(seg)
        # 目标字数下限 100：过小的章节也需容纳"观点 + 至少 1 个例子"
        target = max(int(len(seg) * ratio), 100)
        parts: list[str] = []
        error = ""
        for j, chunk in enumerate(chunks):
            sub_title = title if len(chunks) == 1 else f"{title}（{j+1}/{len(chunks)}）"
            user = _user_prompt(book_title, i + 1, len(units), sub_title, chunk, target // len(chunks))
            try:
                if use_fake:
                    out = _fake_call(system, user, api_key)
                else:
                    out = await _call_deepseek(system, user, api_key)
                result.api_calls += 1
                parts.append(out)
            except Exception as exc:
                error = str(exc)
                result.errors.append(f"{sub_title}：{exc}")
                break
        output = "\n\n".join(parts)
        if output.strip():
            if len(output) > target * 2:
                result.errors.append(
                    f"{title}：输出字数显著超出目标（{len(output)} vs {target}）"
                )
            elif len(output) < target * 0.4:
                result.errors.append(
                    f"{title}：输出字数显著少于目标（{len(output)} vs {target}）"
                )
        result.chapters.append(
            ChapterDistill(
                title=title,
                source_chars=len(seg),
                target_chars=target,
                output_chars=len(output),
                text=output,
                error=error,
            )
        )

    result.total_source_chars = sum(c.source_chars for c in result.chapters)
    result.total_output_chars = sum(c.output_chars for c in result.chapters)
    if progress:
        progress("完成", len(units), len(units))
    return result
