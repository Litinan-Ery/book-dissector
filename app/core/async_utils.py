"""有上限且保持输入顺序的异步并发帮助函数。"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from typing import TypeVar

ItemT = TypeVar("ItemT")
ResultT = TypeVar("ResultT")


async def bounded_map(
    items: Sequence[ItemT],
    worker: Callable[[ItemT], Awaitable[ResultT]],
    *,
    limit: int,
) -> list[ResultT]:
    if limit < 1:
        raise ValueError("concurrency limit must be at least 1")
    semaphore = asyncio.Semaphore(limit)

    async def guarded(item: ItemT) -> ResultT:
        async with semaphore:
            return await worker(item)

    return list(await asyncio.gather(*(guarded(item) for item in items)))

