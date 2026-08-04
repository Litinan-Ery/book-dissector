import asyncio

import pytest

from app.core.retry import retry_async


class TemporaryError(RuntimeError):
    pass


def test_retry_uses_exponential_backoff_then_returns_success() -> None:
    attempts = 0
    delays: list[float] = []

    async def operation() -> str:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise TemporaryError("temporary")
        return "ok"

    async def fake_sleep(delay: float) -> None:
        delays.append(delay)

    result = asyncio.run(
        retry_async(
            operation,
            should_retry=lambda error: isinstance(error, TemporaryError),
            max_attempts=3,
            base_delay=0.25,
            sleep=fake_sleep,
        )
    )

    assert result == "ok"
    assert attempts == 3
    assert delays == [0.25, 0.5]


def test_non_transient_error_is_not_retried() -> None:
    attempts = 0

    async def operation() -> str:
        nonlocal attempts
        attempts += 1
        raise ValueError("permanent")

    with pytest.raises(ValueError):
        asyncio.run(
            retry_async(
                operation,
                should_retry=lambda error: isinstance(error, TemporaryError),
                max_attempts=3,
                base_delay=0,
            )
        )
    assert attempts == 1

