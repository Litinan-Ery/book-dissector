"""精华导出为 Markdown（FR-4）。

元信息头（FR-4.2）：书名 / 作者 / 原格式 / 拆解日期 / 压缩强度 / 保留比例 / API 调用次数。
多次导出不覆盖（FR-4.4）：文件名带时间戳。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from .. import config

EXPORT_SUFFIX = "_精华"


def build_export_md(book_id: str) -> str:
    """构建导出内容：元信息头 + 精华正文。"""
    meta_path = config.BOOKS_DIR / f"{book_id}.meta.json"
    distill_path = config.INTERMEDIATE_DIR / f"{book_id}.distilled.md"
    distill_meta_path = config.INTERMEDIATE_DIR / f"{book_id}.distill.json"

    if not distill_path.exists():
        raise FileNotFoundError("尚未完成拆解，无法导出（请先执行拆解）")

    book_meta: dict = {}
    if meta_path.exists():
        try:
            book_meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            book_meta = {}
    distill_meta: dict = {}
    if distill_meta_path.exists():
        try:
            distill_meta = json.loads(distill_meta_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            distill_meta = {}

    title = book_meta.get("title") or book_id
    author = book_meta.get("author") or "未知"
    source_format = book_meta.get("source_format") or "未知"
    strength = distill_meta.get("strength") or "standard"
    source_chars = distill_meta.get("total_source_chars", 0)
    output_chars = distill_meta.get("total_output_chars", 0)
    api_calls = distill_meta.get("api_calls", 0)
    kept_ratio = (output_chars / source_chars) if source_chars else 0.0
    errors = distill_meta.get("errors", [])
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    strength_names = {
        "conservative": "保守（约 25%）",
        "standard": "标准（约 15%）",
        "aggressive": "激进（约 8%）",
    }

    header = [
        "---",
        f"书名: {title}",
        f"作者: {author}",
        f"原格式: {source_format}",
        f"拆解日期: {date_str}",
        f"压缩强度: {strength_names.get(strength, strength)}",
        f"保留比例: {kept_ratio * 100:.1f}%",
        f"原文约 {source_chars} 字 → 精华 {output_chars} 字",
        f"API 调用: {api_calls} 次",
    ]
    if errors:
        header.append(f"提示: {len(errors)} 处章节有告警（详见拆解结果）")
    header.append("---")

    body = distill_path.read_text(encoding="utf-8").strip()
    return "\n".join(header) + "\n\n" + body + "\n"


def export_book(book_id: str) -> Path:
    """生成导出文件到 storage/output/，返回文件路径。"""
    content = build_export_md(book_id)
    config.ensure_dirs()

    meta_path = config.BOOKS_DIR / f"{book_id}.meta.json"
    title = book_id
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            title = meta.get("title") or book_id
        except (json.JSONDecodeError, OSError):
            pass

    # 文件名安全化：去除路径分隔符与危险字符
    safe_title = "".join(c for c in title if c not in '/\\:*?"<>|').strip() or book_id
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{safe_title}{EXPORT_SUFFIX}_{stamp}.md"
    dest = config.OUTPUT_DIR / filename

    # 极端情况：同一秒多次导出加序号
    n = 2
    while dest.exists():
        dest = config.OUTPUT_DIR / f"{safe_title}{EXPORT_SUFFIX}_{stamp}_{n}.md"
        n += 1

    dest.write_text(content, encoding="utf-8")
    return dest
