"""可测试的异步指数退避。"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import TypeVar

ResultT = TypeVar("ResultT")


async def retry_async(
    operation: Callable[[], Awaitable[ResultT]],
    *,
    should_retry: Callable[[Exception], bool],
    max_attempts: int,
    base_delay: float,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    before_attempt: Callable[[], None] | None = None,
) -> ResultT:
    if max_attempts < 1:
        raise ValueError("max_attempts must be at least 1")
    attempt = 0
    while True:
        attempt += 1
        if before_attempt:
            before_attempt()
        try:
            return await operation()
        except Exception as exc:
            if attempt >= max_attempts or not should_retry(exc):
                raise
            await sleep(base_delay * (2 ** (attempt - 1)))

