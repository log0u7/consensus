"""Tests for governor.py: rate-limit, retry, fallback chain."""

import httpx
import pytest
from src import governor, quota


@pytest.fixture(autouse=True)
def clear_limiters():
    """Reset aiolimiter cache between tests to avoid cross-loop reuse."""
    governor._LIMITERS.clear()
    yield
    governor._LIMITERS.clear()


@pytest.fixture(autouse=True)
def reset_quota():
    """Exhausting retries auto-enables low-quota; never leak that state."""
    quota.set_low_quota(False)
    yield
    quota.set_low_quota(False)


@pytest.mark.asyncio
async def test_call_succeeds_first_try():
    async def ok():
        return "result"

    out = await governor.call("zen", ok)
    assert out == "result"


def test_limiter_caps_per_provider(monkeypatch):
    """One AsyncLimiter per provider: max(RPM, 10000-unlimited-floor), 60s window."""
    monkeypatch.setenv("RPM_ZEN", "60000")  # above the floor -> visible in max_rate
    monkeypatch.setenv("RPM_LOCAL", "0")  # 0 means unlimited -> floor value
    governor._LIMITERS.clear()

    zen = governor._limiter("zen")
    local = governor._limiter("local")
    assert zen.max_rate == 60000
    assert local.max_rate == 10000  # the unlimited floor
    assert zen.time_period == 60
    assert zen is governor._limiter("zen")  # cached per provider
    governor._LIMITERS.clear()


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


# ---------------------------------------------------------------------------
# Retry layering: HTTP-status retries belong to the transport only
# ---------------------------------------------------------------------------


def test_is_retryable_connection_errors():
    assert governor._is_retryable(httpx.ConnectError("refused")) is True
    assert governor._is_retryable(httpx.ReadTimeout("slow")) is True
    assert governor._is_retryable(httpx.RemoteProtocolError("reset")) is True


def test_is_retryable_http_status_not_retried():
    """429/503 are retried by llm._post_with_retry (honours Retry-After);
    the governor must not retry them again on top."""
    req = httpx.Request("POST", "http://x")
    for status in config_retry_statuses():
        exc = httpx.HTTPStatusError(
            f"{status}", request=req, response=httpx.Response(status, request=req)
        )
        assert governor._is_retryable(exc) is False, status
    # Non-retryable statuses are equally not governor-retried
    exc = httpx.HTTPStatusError("500", request=req, response=httpx.Response(500, request=req))
    assert governor._is_retryable(exc) is False


def test_is_retryable_other_exceptions():
    assert governor._is_retryable(RuntimeError("boom")) is False
    assert governor._is_retryable(ValueError("bad json")) is False


def config_retry_statuses():
    from src import config

    return sorted(config.RATE_LIMIT_RETRY_STATUSES)


# ---------------------------------------------------------------------------
# rpm(): explicit rate-limit acquisition (streaming paths)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rpm_acquires_and_releases():
    async with governor.rpm("zen"):
        inside = True
    assert inside
