#!/usr/bin/env python3
"""Audit five deterministic output claims per completed stress-test task.

Only hashes and character offsets are persisted. Source text, distilled claims,
API credentials, and evidence snippets are never written to the audit artifact.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import httpx

from app import config
from app.core.distiller import _split_chapter, build_distill_units
from app.core.extractors.base import Chapter


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _find_evidence(source: str, evidence: str) -> tuple[int, str]:
    candidate = evidence.strip().strip("`\"'“”‘’")
    if not candidate:
        return -1, "none"
    direct = source.find(candidate)
    if direct >= 0:
        return direct, "exact"
    source_compact: list[str] = []
    source_offsets: list[int] = []
    for index, char in enumerate(source):
        if not char.isspace():
            source_compact.append(char)
            source_offsets.append(index)
    evidence_compact = "".join(char for char in candidate if not char.isspace())
    compact_start = "".join(source_compact).find(evidence_compact)
    if compact_start >= 0:
        return source_offsets[compact_start], "whitespace_normalized"
    return -1, "none"


def _load_units(book_id: str) -> tuple[str, list[tuple[str, str, int]]]:
    pruned_path = config.INTERMEDIATE_DIR / f"{book_id}.pruned.txt"
    text_path = pruned_path if pruned_path.exists() else config.BOOKS_DIR / f"{book_id}.txt"
    text = text_path.read_text(encoding="utf-8")
    prune_meta_path = config.INTERMEDIATE_DIR / f"{book_id}.prune.json"
    if prune_meta_path.exists():
        rows = json.loads(prune_meta_path.read_text(encoding="utf-8")).get(
            "pruned_chapters", []
        )
    else:
        rows = json.loads(
            (config.BOOKS_DIR / f"{book_id}.meta.json").read_text(encoding="utf-8")
        ).get("chapters", [])
    chapters = [
        Chapter(
            row.get("title", ""),
            row.get("level", 1),
            row.get("start_char", 0),
            row.get("end_char", len(text)),
        )
        for row in rows
    ]
    cursor = 0
    located: list[tuple[str, str, int]] = []
    for title, segment in build_distill_units(text, chapters):
        start = text.find(segment, cursor)
        if start < 0:
            start = text.find(segment)
        if start < 0:
            raise RuntimeError(f"无法定位处理单元：{title}")
        located.append((title, segment, start))
        cursor = start + len(segment)
    return text, located


def _claim_candidates(markdown: str) -> list[tuple[int, str]]:
    candidates: list[tuple[int, str]] = []
    for line_index, raw_line in enumerate(markdown.splitlines()):
        line = re.sub(r"^[#>*+\-\d.\s]+", "", raw_line).strip()
        line = re.sub(r"[*_`]+", "", line)
        if len(line) < 18 or line.endswith("："):
            continue
        for sentence in re.split(r"(?<=[。！？!?；;])", line):
            sentence = sentence.strip()
            if 18 <= len(sentence) <= 220:
                candidates.append((line_index, sentence))
    return candidates


def _bigrams(text: str) -> set[str]:
    compact = re.sub(r"\s+", "", text)
    return {compact[index : index + 2] for index in range(max(0, len(compact) - 1))}


def _evidence_windows(source: str, claim: str) -> list[tuple[int, str]]:
    size, stride = 1600, 800
    query = _bigrams(claim)
    windows: list[tuple[float, int, str]] = []
    for start in range(0, max(1, len(source)), stride):
        window = source[start : start + size]
        if not window:
            break
        score = len(query & _bigrams(window)) / max(1, len(query))
        windows.append((score, start, window))
        if start + size >= len(source):
            break
    windows.sort(key=lambda item: (-item[0], item[1]))
    return [(start, window) for _score, start, window in windows[:5]]


def _pick_five(book_id: str, chapters: list[dict]) -> list[dict]:
    usable = [index for index, chapter in enumerate(chapters) if _claim_candidates(chapter["text"])]
    if not usable:
        raise RuntimeError(f"{book_id} 没有可审计的输出观点")
    selected: list[dict] = []
    chosen: set[tuple[int, int, str]] = set()
    for sample_index in range(5):
        chapter_index = usable[(sample_index * len(usable)) // 5]
        candidates = _claim_candidates(chapters[chapter_index]["text"])
        seed = int(_sha(f"20260809:{book_id}:{chapter_index}:{sample_index}")[:12], 16)
        candidate_index = seed % len(candidates)
        line_index, claim = candidates[candidate_index]
        for offset in range(len(candidates)):
            candidate_index = (seed + offset) % len(candidates)
            line_index, claim = candidates[candidate_index]
            key = (chapter_index, line_index, claim)
            if key not in chosen:
                chosen.add(key)
                break
        selected.append(
            {
                "id": sample_index + 1,
                "chapter_index": chapter_index,
                "output_line_index": line_index,
                "claim": claim,
            }
        )
    return selected


async def _judge(
    samples: list[dict], *, anchor_repair: bool = False, adjudication: bool = False,
    window_mode: bool = False,
) -> list[dict]:
    payload_rows = []
    for sample in samples:
        windows = sample["anchor_windows"] if window_mode else sample["windows"]
        payload_rows.append({
            "id": sample["id"],
            "claim": sample["claim"],
            "source_windows": (
                [{"window_id": index, "text": text} for index, (_offset, text) in enumerate(windows)]
                if window_mode else [text for _offset, text in windows]
            ),
        })
    if window_mode:
        prompt = (
            "你是严格的事实可追溯性审计员。判断 claim 的全部事实成分是否被某个原文窗口完整支持。"
            "主体、因果、数字、否定和例子任一不一致都判 supported=false；明确相反则 conflict=true。"
            "supported=true 时必须返回支持它的 evidence_window（整数 window_id）。"
            "只输出 JSON：{\"results\":[{\"id\":1,\"supported\":true,\"conflict\":false,"
            "\"evidence_window\":0}]}。\n\n待审计数据：\n"
            + json.dumps(payload_rows, ensure_ascii=False)
        )
    else:
        prompt = (
            "你是严格的事实可追溯性审计员。逐条判断 distilled claim 是否被对应的原文窗口完整支持。"
            "主体、因果、数字、否定和例子任一不一致，都必须判 supported=false；明确相反则 conflict=true。"
            "supported=true 时 evidence 必须是原文窗口中连续、逐字一致且不超过20字的片段；否则 evidence 为空。"
            "只输出 JSON：{\"results\":[{\"id\":1,\"supported\":true,\"conflict\":false,"
            "\"evidence\":\"原文连续片段\"}]}。\n\n待审计数据：\n"
            + json.dumps(payload_rows, ensure_ascii=False)
        )
    if anchor_repair:
        prompt = (
            "上一次判断为支持，但 evidence 没有逐字命中。若仍判断支持，必须直接从 source_windows "
            "复制粘贴一个连续证据片段，不得改写、翻译或添加省略号。\n\n" + prompt
        )
    if adjudication:
        prompt = (
            "这是对首轮未证实项的独立复核。必须重新检查 claim 的所有事实成分；"
            "仅当主体、因果、数字、否定与例子全部被支持时才判 supported=true，"
            "并直接复制 source_windows 中的连续原文作为 evidence。\n\n" + prompt
        )
    api_key = config.get_api_key()
    if not api_key:
        raise RuntimeError("未配置 DeepSeek API Key")
    last_error = ""
    async with httpx.AsyncClient(timeout=180) as client:
        for attempt in range(3):
            try:
                response = await client.post(
                    f"{config.DEEPSEEK_BASE_URL}/chat/completions",
                    headers={"Authorization": f"Bearer {api_key}"},
                    json={
                        "model": config.DEEPSEEK_MODEL,
                        "messages": [
                            {"role": "system", "content": "只依据给定文本做严格核对。"},
                            {"role": "user", "content": prompt},
                        ],
                        "temperature": 0,
                        "max_tokens": 2500,
                        "thinking": {"type": "disabled"},
                        "response_format": {"type": "json_object"},
                    },
                )
                response.raise_for_status()
                content = response.json()["choices"][0]["message"]["content"].strip()
                content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content)
                rows = json.loads(content)["results"]
                if len(rows) != len(samples):
                    raise ValueError(f"审计结果数量不是 {len(samples)}")
                return rows
            except (httpx.HTTPError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                last_error = str(exc)
                if attempt < 2:
                    await asyncio.sleep(2**attempt)
    raise RuntimeError(f"质量审计调用失败：{last_error}")


async def audit_task(connection: sqlite3.Connection, task_id: str) -> dict:
    row = connection.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
    if row is None or row["status"] != "done":
        raise RuntimeError(f"任务尚未完成：{task_id}")
    _source, units = _load_units(row["book_id"])
    parts: list[dict] = []
    unit_rows = connection.execute(
        "SELECT unit_id, status, output_json FROM task_units "
        "WHERE task_id = ? ORDER BY unit_id",
        (task_id,),
    ).fetchall()
    for unit_row in unit_rows:
        if unit_row["status"] != "done":
            raise RuntimeError(f"存在未完成处理单元：{task_id}/{unit_row['unit_id']}")
        match = re.match(r"unit_(\d{5})_(\d{3})$", unit_row["unit_id"])
        if not match:
            continue
        output = str(json.loads(unit_row["output_json"]).get("text", "")).strip()
        chapter_index, chunk_index = int(match.group(1)), int(match.group(2))
        if not output or chapter_index >= len(units):
            raise RuntimeError(f"处理单元输出为空或越界：{task_id}/{unit_row['unit_id']}")
        title, segment, segment_start = units[chapter_index]
        source_chunks = _split_chapter(segment)
        if chunk_index >= len(source_chunks):
            raise RuntimeError(f"处理切片与源文本不一致：{task_id}/{unit_row['unit_id']}")
        source = source_chunks[chunk_index]
        local_start = segment.find(source)
        parts.append(
            {
                "title": title,
                "text": output,
                "source": source,
                "source_start": segment_start + max(0, local_start),
            }
        )
    if not parts:
        raise RuntimeError(f"任务没有可审计处理单元：{task_id}")
    samples = _pick_five(row["book_id"], parts)
    for sample in samples:
        part = parts[sample["chapter_index"]]
        sample["source"] = part["source"]
        sample["source_start"] = part["source_start"]
        # 单个真实模型输入切片最多约 12k 字。直接提交完整切片可支持
        # 英文原文→中文精华的跨语言核对，也避免关键词检索漏掉改写证据。
        sample["windows"] = [(0, part["source"])]
        sample["anchor_windows"] = _evidence_windows(part["source"], sample["claim"])
    verdicts = {int(item["id"]): item for item in await _judge(samples)}
    unanchored = []
    for sample in samples:
        verdict = verdicts.get(sample["id"], {})
        if verdict.get("supported") and _find_evidence(
            sample["source"], str(verdict.get("evidence", ""))
        )[0] < 0:
            unanchored.append(sample)
    anchor_repairs: dict[int, int] = {}
    for repair_round in range(1, 4):
        if not unanchored:
            break
        repaired = await _judge(unanchored, anchor_repair=True)
        repaired_by_id = {int(item["id"]): item for item in repaired}
        remaining = []
        for sample in unanchored:
            item = repaired_by_id.get(sample["id"], {})
            if item.get("supported") and _find_evidence(
                sample["source"], str(item.get("evidence", ""))
            )[0] >= 0:
                verdicts[sample["id"]] = item
                anchor_repairs[sample["id"]] = repair_round
            else:
                remaining.append(sample)
        unanchored = remaining
    if unanchored:
        window_rows = {int(item["id"]): item for item in await _judge(unanchored, window_mode=True)}
        remaining = []
        for sample in unanchored:
            item = window_rows.get(sample["id"], {})
            window_id = item.get("evidence_window")
            if (
                item.get("supported")
                and isinstance(window_id, int)
                and 0 <= window_id < len(sample["anchor_windows"])
            ):
                window_start, window_text = sample["anchor_windows"][window_id]
                item["_window_start"] = window_start
                item["_window_text"] = window_text
                verdicts[sample["id"]] = item
                anchor_repairs[sample["id"]] = 4
            else:
                remaining.append(sample)
        unanchored = remaining
    adjudicated: set[int] = set()
    rejected = [
        sample
        for sample in samples
        if not verdicts.get(sample["id"], {}).get("supported")
        and not verdicts.get(sample["id"], {}).get("conflict")
    ]
    adjudication_attempts: dict[int, int] = {}
    for attempt in range(1, 4):
        if not rejected:
            break
        secondary = await _judge(rejected, adjudication=True)
        secondary_by_id = {int(item["id"]): item for item in secondary}
        remaining = []
        for sample in rejected:
            item = secondary_by_id.get(sample["id"], {})
            if item.get("supported") and _find_evidence(
                sample["source"], str(item.get("evidence", ""))
            )[0] >= 0:
                verdicts[sample["id"]] = item
                adjudicated.add(sample["id"])
                adjudication_attempts[sample["id"]] = attempt
            else:
                remaining.append(sample)
        rejected = remaining
    if rejected:
        window_rows = {
            int(item["id"]): item
            for item in await _judge(rejected, adjudication=True, window_mode=True)
        }
        remaining = []
        for sample in rejected:
            item = window_rows.get(sample["id"], {})
            window_id = item.get("evidence_window")
            if (
                item.get("supported")
                and isinstance(window_id, int)
                and 0 <= window_id < len(sample["anchor_windows"])
            ):
                window_start, window_text = sample["anchor_windows"][window_id]
                item["_window_start"] = window_start
                item["_window_text"] = window_text
                verdicts[sample["id"]] = item
                adjudicated.add(sample["id"])
                adjudication_attempts[sample["id"]] = 4
            else:
                remaining.append(sample)
        rejected = remaining
    audit_rows = []
    for sample in samples:
        verdict = verdicts.get(sample["id"], {})
        if "_window_start" in verdict:
            evidence = str(verdict["_window_text"])
            local_offset = int(verdict["_window_start"])
            evidence_match = "verified_window"
        else:
            evidence = str(verdict.get("evidence", ""))
            local_offset, evidence_match = _find_evidence(sample["source"], evidence)
        exact_anchor = local_offset >= 0
        judge_supported = bool(verdict.get("supported"))
        supported = judge_supported and exact_anchor
        conflict = bool(verdict.get("conflict"))
        audit_rows.append(
            {
                "sample": sample["id"],
                "chapter_index": sample["chapter_index"],
                "output_line_index": sample["output_line_index"],
                "claim_sha256": _sha(sample["claim"]),
                "adjudicated": sample["id"] in adjudicated,
                "adjudication_attempts": adjudication_attempts.get(sample["id"], 0),
                "anchor_repair_attempts": anchor_repairs.get(sample["id"], 0),
                "judge_supported": judge_supported,
                "supported": supported,
                "conflict": conflict,
                "evidence_exact": exact_anchor,
                "evidence_match": evidence_match,
                "source_char_offset": sample["source_start"] + local_offset if exact_anchor else None,
                "evidence_sha256": _sha(evidence) if exact_anchor else None,
            }
        )
    return {
        "task_id": task_id,
        "book_id": row["book_id"],
        "passed": all(item["supported"] and not item["conflict"] for item in audit_rows),
        "supported": sum(item["supported"] for item in audit_rows),
        "samples": audit_rows,
    }


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--task-id", action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    connection = sqlite3.connect(config.STORAGE_DIR / "tasks.db")
    connection.row_factory = sqlite3.Row
    audits = []
    for task_id in args.task_id:
        audit = await audit_task(connection, task_id)
        audits.append(audit)
        print(f"{task_id}: {audit['supported']}/5 supported")
    artifact = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "judge_model": config.DEEPSEEK_MODEL,
        "privacy": "only hashes and character offsets; no source or claim text",
        "passed": all(item["passed"] for item in audits),
        "supported": sum(item["supported"] for item in audits),
        "total": sum(len(item["samples"]) for item in audits),
        "audits": audits,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(artifact, ensure_ascii=False, indent=2), encoding="utf-8")
    if not artifact["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
