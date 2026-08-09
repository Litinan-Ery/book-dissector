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

import asyncio
import json
import os
import re
import hashlib
from dataclasses import dataclass, field
from typing import Callable

import httpx

from .. import config
from .extractors.base import Chapter
from .task_store import TaskStore

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
    cache_hits: int = 0
    errors: list[str] = field(default_factory=list)
    modality_warnings: list[dict] = field(default_factory=list)

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


def _output_length_issue(output: str, target_chars: int) -> str:
    """返回单元输出长度问题；短尾章允许固定字数的排版与语义开销。"""
    actual = len(output)
    # Markdown 标题和固定小节会让很短的单元天然占用数百字。仅按倍数
    # 判定会把全书压缩比合格的结果因一个尾章误判失败，因此同时要求
    # 超过 2 倍目标且绝对超额超过 500 字。
    if actual > max(target_chars * 2, target_chars + 500):
        return f"输出字数显著超出目标（{actual} vs {target_chars}）"
    if actual < target_chars * 0.75:
        return f"输出字数显著少于目标（{actual} vs {target_chars}）"
    return ""


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


def build_distill_units(text: str, chapters: list[Chapter]) -> list[tuple[str, str]]:
    """按最高结构层级构建蒸馏单元。

    提取器会记录章、节、小节的所有标题。如果把每个标题都当成
    独立模型请求，会将结构扁平化，并使短小节的调用量失控。
    因此只用当前最高层级的标题切分全文，其下层标题和正文
    作为该章的完整内容一起交给模型。
    """
    valid = [
        chapter
        for chapter in chapters
        if 0 <= chapter.start_char < len(text)
    ]
    if not valid:
        return [
            (f"第 {index + 1} 部分", segment)
            for index, segment in enumerate(_split_chapter(text))
            if segment.strip()
        ]

    top_level = min(chapter.level for chapter in valid)
    top_chapters = sorted(
        (chapter for chapter in valid if chapter.level == top_level),
        key=lambda chapter: chapter.start_char,
    )
    units: list[tuple[str, str]] = []
    for index, chapter in enumerate(top_chapters):
        end = (
            top_chapters[index + 1].start_char
            if index + 1 < len(top_chapters)
            else len(text)
        )
        segment = text[chapter.start_char:end]
        if segment.strip():
            units.append((chapter.title or f"章节 {index + 1}", segment))
    return units


def count_distill_calls(text: str, chapters: list[Chapter]) -> int:
    """使用与真实蒸馏完全相同的分章/切片规则计算请求数。"""
    return sum(
        len(_split_chapter(segment))
        for _title, segment in build_distill_units(text, chapters)
    )


async def _call_deepseek(system: str, user: str, api_key: str) -> str:
    """调用 DeepSeek chat completions，对短暂错误、空内容和截断做有上限重试。"""
    max_tokens = 4096
    last_error = "DeepSeek 请求失败"
    async with httpx.AsyncClient(timeout=180) as client:
        for attempt in range(3):
            payload = {
                "model": config.DEEPSEEK_MODEL,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "temperature": 0.3,
                "max_tokens": max_tokens,
                # V4 Flash 默认会生成 reasoning_content；蒸馏输出
                # 只需要最终 Markdown，禁用 thinking 避免它占用时间和 token。
                "thinking": {"type": "disabled"},
            }
            try:
                resp = await client.post(
                    f"{config.DEEPSEEK_BASE_URL}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = f"DeepSeek 网络错误：{exc.__class__.__name__}"
                if attempt < 2:
                    await asyncio.sleep(2**attempt)
                    continue
                raise RuntimeError(last_error) from exc

            if resp.status_code == 401:
                raise RuntimeError("API Key 无效（401），请在设置中重新填写")
            if resp.status_code == 429 or resp.status_code >= 500:
                last_error = f"DeepSeek API 暂时错误（HTTP {resp.status_code}）"
                if attempt < 2:
                    await asyncio.sleep(2**attempt)
                    continue
                raise RuntimeError(last_error)
            if resp.status_code != 200:
                raise RuntimeError(
                    f"DeepSeek API 错误（HTTP {resp.status_code}）：{resp.text[:200]}"
                )
            try:
                data = resp.json()
                choice = data["choices"][0]
                content = choice["message"]["content"].strip()
                finish_reason = choice.get("finish_reason", "")
            except (ValueError, KeyError, IndexError, TypeError, AttributeError):
                content = ""
                finish_reason = ""

            if content and finish_reason != "length":
                return content
            if finish_reason == "length":
                last_error = "DeepSeek 输出被 max_tokens 截断"
                max_tokens = min(max_tokens * 2, 16384)
            else:
                last_error = "DeepSeek 响应为空或格式异常"
            if attempt < 2:
                await asyncio.sleep(2**attempt)
                continue
    raise RuntimeError(last_error)


def _fake_call(system: str, user: str, api_key: str) -> str:
    """本地假实现（测试用，不真实调用 API）。"""
    m = re.search(r"第 (\d+)/(\d+) 章「(.+?)」", user)
    title = m.group(3) if m else "未知章节"
    output = f"## {title}\n\n**核心观点**：原文观点（FAKE 测试输出，不真实调用）。\n\n**例子**：原文中的示例。"  # noqa: E501
    target_match = re.search(r"压缩目标约 (\d+) 字", user)
    minimum = int(target_match.group(1)) * 4 // 5 if target_match else 0
    if len(output) < minimum:
        output += "\n\n" + ("补充测试要点。" * ((minimum - len(output)) // 7 + 1))
    return output


async def distill_book(
    book_id: str,
    book_type: str = "general",
    strength: str = DEFAULT_STRENGTH,
    progress: ProgressCb | None = None,
    use_fake: bool | None = None,
    task_store: TaskStore | None = None,
    task_id: str | None = None,
    should_interrupt: Callable[[], bool] | None = None,
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

    # 只按最高层级章节分单元；章内的节/小节保留在同一语境中。
    units = build_distill_units(text, chapters)

    ratio = _ratio_for(book_type, strength)
    result = DistillResult(
        book_title=book_title,
        book_type=book_type,
        strength=strength,
        modality_warnings=meta.get("modality_warnings", []),
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
            chunk_target = max(1, target // len(chunks))
            user = _user_prompt(
                book_title, i + 1, len(units), sub_title, chunk, chunk_target
            )
            unit_id = f"unit_{i:05d}_{j:03d}"
            cache_key = hashlib.sha256(
                json.dumps(
                    {
                        "book_id": book_id,
                        "book_type": book_type,
                        "strength": strength,
                        "model": config.DEEPSEEK_MODEL,
                        "unit_id": unit_id,
                        "text": chunk,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ).encode("utf-8")
            ).hexdigest()
            try:
                if should_interrupt and should_interrupt():
                    raise DistillInterrupted("服务正在关闭，已停止创建新的模型请求")
                if task_store and task_id and task_store.is_cancel_requested(task_id):
                    raise DistillCancelled("任务已取消，停止创建新的模型请求")
                out = ""
                if task_store and task_id:
                    checkpoint = next(
                        (item for item in task_store.get_units(task_id) if item.unit_id == unit_id),
                        None,
                    )
                    if (
                        checkpoint
                        and checkpoint.status == "done"
                        and checkpoint.cache_key == cache_key
                    ):
                        out = str(checkpoint.output.get("text", ""))
                    if not out:
                        cached = task_store.get_cache(cache_key)
                        if cached:
                            out = str(cached.get("text", ""))
                    # 旧检查点可能在长度校验之前被写为 done。
                    # 不复用不合格缓存，但保留其它已验证单元。
                    if out and _output_length_issue(out, chunk_target):
                        out = ""
                    if out:
                        result.cache_hits += 1
                        task_store.upsert_unit(
                            task_id, unit_id, "done", cache_key, output={"text": out}
                        )
                    else:
                        attempts = (checkpoint.attempts if checkpoint else 0) + 1
                        task_store.upsert_unit(
                            task_id, unit_id, "running", cache_key, attempts=attempts
                        )
                if not out:
                    for length_attempt in range(3):
                        # A length retry is a brand-new model request. Re-check
                        # persisted stop/delete state after every completed call
                        # so a delete received during that call cannot start the
                        # next retry.
                        if task_store and task_id and task_store.is_cancel_requested(task_id):
                            raise DistillCancelled("任务已取消，停止创建新的模型请求")
                        if should_interrupt and should_interrupt():
                            raise DistillInterrupted("服务正在关闭，已停止创建新的模型请求")
                        if use_fake:
                            out = _fake_call(system, user, api_key)
                        else:
                            out = await _call_deepseek(system, user, api_key)
                        result.api_calls += 1
                        issue = _output_length_issue(out, chunk_target)
                        if not issue:
                            break
                        if length_attempt < 2:
                            minimum_chars = max(1, int(chunk_target * 0.8))
                            maximum_chars = max(minimum_chars, int(chunk_target * 1.25))
                            user += (
                                f"\n\n上一版{issue}。请重新输出完整 Markdown，"
                                f"正文必须达到 {minimum_chars}–{maximum_chars} 字，"
                                "从原文补充必要的关键论据、具体事实和代表例子，"
                                "不得注水、不得写修改说明。"
                            )
                            continue
                        raise RuntimeError(issue)
                    if task_store and task_id:
                        task_store.upsert_unit(
                            task_id,
                            unit_id,
                            "done",
                            cache_key,
                            attempts=(checkpoint.attempts if checkpoint else 0) + 1,
                            output={"text": out},
                        )
                        task_store.put_cache(
                            cache_key,
                            {"text": out},
                            book_id=book_id,
                        )
                        if task_store.is_cancel_requested(task_id):
                            raise DistillCancelled("任务已取消；已完成单元已保存，可稍后恢复")
                        if should_interrupt and should_interrupt():
                            raise DistillInterrupted("服务正在关闭；已完成单元已保存")
                parts.append(out)
            except (DistillCancelled, DistillInterrupted):
                raise
            except Exception as exc:
                error = str(exc)
                result.errors.append(f"{sub_title}：{exc}")
                if task_store and task_id:
                    checkpoint = next(
                        (item for item in task_store.get_units(task_id) if item.unit_id == unit_id),
                        None,
                    )
                    task_store.upsert_unit(
                        task_id,
                        unit_id,
                        "error",
                        cache_key,
                        attempts=(checkpoint.attempts if checkpoint else 0) + 1,
                        error=str(exc),
                    )
                break
        output = "\n\n".join(parts)
        if output.strip():
            issue = _output_length_issue(output, target)
            if issue:
                result.errors.append(f"{title}：{issue}")
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


class DistillCancelled(RuntimeError):
    """用户取消任务后，在创建下一次模型请求前中止。"""


class DistillInterrupted(RuntimeError):
    """服务关闭时在完成当前检查点后中止，供下次启动恢复。"""
