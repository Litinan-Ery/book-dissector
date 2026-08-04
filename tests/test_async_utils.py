import asyncio

from app.core.async_utils import bounded_map


def test_bounded_map_limits_concurrency_and_preserves_input_order() -> None:
    active = 0
    peak = 0

    async def worker(value: int) -> int:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01 * (4 - value))
        active -= 1
        return value * 10

    result = asyncio.run(bounded_map([1, 2, 3], worker, limit=2))

    assert result == [10, 20, 30]
    assert peak == 2

