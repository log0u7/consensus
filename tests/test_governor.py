"""Tests for governor.py: rate-limit, retry, fallback chain."""

import pytest
from src import governor


@pytest.fixture(autouse=True)
def clear_limiters():
    """Reset aiolimiter cache between tests to avoid cross-loop reuse."""
    if governor._HAS_LIMITER:
        governor._LIMITERS.clear()
    yield
    if governor._HAS_LIMITER:
        governor._LIMITERS.clear()


@pytest.mark.asyncio
async def test_call_succeeds_first_try():
    async def ok():
        return "result"

    out = await governor.call("zen", ok)
    assert out == "result"


@pytest.mark.asyncio
async def test_call_uses_fallback_on_failure(monkeypatch):
    """When the primary provider raises, the fallback provider is tried."""
    call_log: list[str] = []

    async def fail():
        call_log.append("primary")
        raise RuntimeError("quota exhausted")

    async def succeed():
        call_log.append("fallback")
        return "fallback_result"

    def factory(pname: str):
        if pname == "zen":
            return fail
        return succeed

    out = await governor.call("zen", fail, fallback=["local"], fallback_factory=factory)
    assert out == "fallback_result"
    assert call_log == ["primary", "fallback"]


@pytest.mark.asyncio
async def test_call_raises_when_all_exhausted():
    async def fail():
        raise RuntimeError("always fails")

    with pytest.raises(RuntimeError, match="always fails"):
        await governor.call("zen", fail, fallback=[])


@pytest.mark.asyncio
async def test_call_no_fallback_raises():
    """With no fallback, the original exception propagates."""
    async def fail():
        raise ValueError("boom")

    with pytest.raises(ValueError, match="boom"):
        await governor.call("zen", fail)
