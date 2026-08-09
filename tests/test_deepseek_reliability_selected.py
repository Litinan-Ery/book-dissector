import asyncio
import json
from pathlib import Path

import httpx
import pytest

from app import config
from app.core import distiller
from app.core.task_store import TaskStore


class _FakeClient:
    def __init__(self, responses: list[httpx.Response], payloads: list[dict]) -> None:
        self.responses = responses
        self.payloads = payloads

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def post(self, _url: str, *, json: dict, **_kwargs) -> httpx.Response:
        self.payloads.append(json)
        return self.responses.pop(0)


def _response(status: int, payload: dict) -> httpx.Response:
    return httpx.Response(
        status,
        json=payload,
        request=httpx.Request("POST", "https://api.deepseek.com/chat/completions"),
    )


def _success(content: str = "## 章节\n精华", finish_reason: str = "stop") -> httpx.Response:
    return _response(
        200,
        {
            "choices": [
                {
                    "finish_reason": finish_reason,
                    "message": {"content": content, "reasoning_content": "不应生成"},
                }
            ]
        },
    )


def _install_client(monkeypatch, responses: list[httpx.Response]) -> list[dict]:
    payloads: list[dict] = []
    monkeypatch.setattr(
        distiller.httpx,
        "AsyncClient",
        lambda **_kwargs: _FakeClient(responses, payloads),
    )

    async def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(distiller.asyncio, "sleep", no_sleep)
    return payloads


def test_deepseek_disables_thinking(monkeypatch) -> None:
    payloads = _install_client(monkeypatch, [_success()])

    result = asyncio.run(distiller._call_deepseek("system", "user", "key"))

    assert result.startswith("## 章节")
    assert payloads[0]["thinking"] == {"type": "disabled"}


def test_deepseek_retries_transient_http_error(monkeypatch) -> None:
    payloads = _install_client(
        monkeypatch,
        [_response(429, {"error": {"message": "busy"}}), _success()],
    )

    assert asyncio.run(distiller._call_deepseek("system", "user", "key"))
    assert len(payloads) == 2


def test_deepseek_retries_truncation_with_larger_budget(monkeypatch) -> None:
    payloads = _install_client(
        monkeypatch,
        [_success("未完成", finish_reason="length"), _success("完整输出")],
    )

    result = asyncio.run(distiller._call_deepseek("system", "user", "key"))

    assert result == "完整输出"
    assert [payload["max_tokens"] for payload in payloads] == [4096, 8192]


def test_deepseek_rejects_repeated_empty_content(monkeypatch) -> None:
    empty = _success("")
    payloads = _install_client(monkeypatch, [empty, _success(""), _success("")])

    with pytest.raises(RuntimeError, match="响应为空"):
        asyncio.run(distiller._call_deepseek("system", "user", "key"))

    assert len(payloads) == 3


def test_distill_interrupt_is_separate_from_user_cancel() -> None:
    assert issubclass(distiller.DistillInterrupted, RuntimeError)
    assert not issubclass(distiller.DistillInterrupted, distiller.DistillCancelled)


def test_short_tail_unit_allows_fixed_markdown_overhead() -> None:
    assert distiller._output_length_issue("字" * 686, 331) == ""
    assert "显著超出" in distiller._output_length_issue("字" * 900, 331)


def test_short_tail_tolerance_is_used_by_chapter_aggregate(
    tmp_path: Path, monkeypatch
) -> None:
    book_id = _configure_book(tmp_path, monkeypatch)

    async def markdown_with_fixed_overhead(
        _system: str, _user: str, _api_key: str
    ) -> str:
        return "字" * 700

    monkeypatch.setattr(distiller, "_call_deepseek", markdown_with_fixed_overhead)

    result = asyncio.run(distiller.distill_book(book_id, use_fake=False))

    assert result.api_calls == 1
    assert result.errors == []


def _configure_book(tmp_path: Path, monkeypatch) -> str:
    books = tmp_path / "books"
    books.mkdir()
    monkeypatch.setattr(config, "BOOKS_DIR", books)
    monkeypatch.setattr(config, "INTERMEDIATE_DIR", tmp_path / "intermediate")
    monkeypatch.setattr(config, "get_api_key", lambda: "test-key")
    book_id = "length-book"
    (books / f"{book_id}.txt").write_text("正文" * 1000, encoding="utf-8")
    (books / f"{book_id}.meta.json").write_text(
        json.dumps({"title": "长度测试书", "chapters": []}),
        encoding="utf-8",
    )
    return book_id


def test_distill_corrects_bad_length_before_caching(tmp_path: Path, monkeypatch) -> None:
    book_id = _configure_book(tmp_path, monkeypatch)
    outputs = iter(["过短", "合格内容" * 60])

    async def fake_call(_system: str, _user: str, _api_key: str) -> str:
        return next(outputs)

    monkeypatch.setattr(distiller, "_call_deepseek", fake_call)

    result = asyncio.run(distiller.distill_book(book_id, use_fake=False))

    assert result.api_calls == 2
    assert result.errors == []
    assert result.chapters[0].output_chars == len("合格内容" * 60)


def test_repeated_bad_length_marks_only_that_unit_failed(
    tmp_path: Path, monkeypatch
) -> None:
    book_id = _configure_book(tmp_path, monkeypatch)

    async def always_short(_system: str, _user: str, _api_key: str) -> str:
        return "过短"

    monkeypatch.setattr(distiller, "_call_deepseek", always_short)
    store = TaskStore(tmp_path / "tasks.db")
    task = store.create_task(book_id, "general", "standard")

    result = asyncio.run(
        distiller.distill_book(
            book_id,
            use_fake=False,
            task_store=store,
            task_id=task.task_id,
        )
    )

    assert result.api_calls == 3
    assert "输出字数显著少于目标" in result.errors[0]
    assert store.get_units(task.task_id)[0].status == "error"
