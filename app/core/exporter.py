"""通过质量门禁的正式 Markdown 导出与显式诊断稿导出。"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from .. import config

EXPORT_SUFFIX = "_精华"


class ExportQualityError(RuntimeError):
    pass


def _load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def build_export_md(book_id: str, *, diagnostic: bool = False) -> str:
    meta_path = config.BOOKS_DIR / f"{book_id}.meta.json"
    distill_path = config.INTERMEDIATE_DIR / f"{book_id}.distilled.md"
    distill_meta_path = config.INTERMEDIATE_DIR / f"{book_id}.distill.json"
    quality_path = config.INTERMEDIATE_DIR / f"{book_id}.quality.json"
    if not distill_path.exists():
        raise FileNotFoundError("尚未完成拆解，无法导出（请先执行拆解）")

    book_meta = _load_json(meta_path)
    distill_meta = _load_json(distill_meta_path)
    quality = _load_json(quality_path)
    quality_status = quality.get("status", "missing")
    blockers = quality.get("blocking_issues", [])
    if quality_status != "pass" and not diagnostic:
        detail = "；".join(blockers) or "缺少有效质量报告"
        raise ExportQualityError(f"质量门禁未通过，只能导出诊断稿：{detail}")

    title = book_meta.get("title") or book_id
    author = book_meta.get("author") or "未知"
    source_format = book_meta.get("source_format") or "未知"
    source_fingerprint = book_meta.get("source_fingerprint") or "未知"
    strength = distill_meta.get("strength") or "standard"
    source_chars = distill_meta.get("total_source_chars", 0)
    output_chars = distill_meta.get("total_output_chars", 0)
    api_calls = distill_meta.get("api_calls", 0)
    kept_ratio = (output_chars / source_chars) if source_chars else 0.0
    body_coverage = quality.get("body_coverage", 0.0)
    anchor_coverage = quality.get("anchor_coverage", 0.0)
    merged_count = quality.get(
        "duplicate_merged_count", distill_meta.get("duplicate_merged_count", 0)
    )
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
        f"源文件指纹: {source_fingerprint}",
        f"拆解日期: {date_str}",
        f"模型: {distill_meta.get('model', config.DEEPSEEK_MODEL)}",
        f"提示词版本: {distill_meta.get('prompt_version', '1.0')}",
        f"压缩强度: {strength_names.get(strength, strength)}",
        f"保留比例: {kept_ratio * 100:.1f}%",
        f"原文约 {source_chars} 字 → 精华 {output_chars} 字",
        f"API 调用: {api_calls} 次",
        f"质量状态: {quality_status}",
        f"正文覆盖率: {body_coverage * 100:.1f}%",
        f"锚点覆盖率: {anchor_coverage * 100:.1f}%",
        f"合并重复知识: {merged_count} 条",
        "---",
    ]
    notice = ""
    if diagnostic:
        reasons = "\n".join(f"- {issue}" for issue in blockers) or "- 缺少有效质量报告"
        notice = (
            "# 未通过质量校验的诊断稿\n\n"
            "本文件不得作为正式精华稿使用。未解决问题：\n"
            f"{reasons}\n\n"
        )
    body = distill_path.read_text(encoding="utf-8").strip()
    return "\n".join(header) + "\n\n" + notice + body + "\n"


def export_book(book_id: str, *, diagnostic: bool = False) -> Path:
    content = build_export_md(book_id, diagnostic=diagnostic)
    config.ensure_dirs()
    meta = _load_json(config.BOOKS_DIR / f"{book_id}.meta.json")
    title = meta.get("title") or book_id
    safe_title = "".join(c for c in title if c not in '/\\:*?"<>|').strip() or book_id
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    suffix = "_诊断" if diagnostic else EXPORT_SUFFIX
    destination = config.OUTPUT_DIR / f"{safe_title}{suffix}_{stamp}.md"
    index = 2
    while destination.exists():
        destination = config.OUTPUT_DIR / f"{safe_title}{suffix}_{stamp}_{index}.md"
        index += 1
    destination.write_text(content, encoding="utf-8")
    return destination

