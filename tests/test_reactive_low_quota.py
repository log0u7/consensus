import httpx
import pytest
from src import config, llm, quota


@pytest.mark.asyncio
async def test_auto_low_quota_on_exhausted_429(monkeypatch):
    monkeypatch.setattr(config, "RATE_LIMIT_BASE_DELAY", 0.0)
    monkeypatch.setattr(config, "RATE_LIMIT_MAX_RETRIES", 1)
    monkeypatch.setattr(config, "AUTO_LOW_QUOTA", True)
    quota.set_low_quota(False)

    def handler(request):
        return httpx.Response(429, json={"detail": "no"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        with pytest.raises(httpx.HTTPStatusError):
            await llm._post_with_retry(c, "http://x/test", json={})
    assert quota.is_low_quota() is True
    quota.set_low_quota(False)


@pytest.mark.asyncio
async def test_auto_low_quota_disabled(monkeypatch):
    monkeypatch.setattr(config, "RATE_LIMIT_BASE_DELAY", 0.0)
    monkeypatch.setattr(config, "RATE_LIMIT_MAX_RETRIES", 1)
    monkeypatch.setattr(config, "AUTO_LOW_QUOTA", False)
    quota.set_low_quota(False)

    def handler(request):
        return httpx.Response(429, json={"detail": "no"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        with pytest.raises(httpx.HTTPStatusError):
            await llm._post_with_retry(c, "http://x/test", json={})
    assert quota.is_low_quota() is False


@pytest.mark.asyncio
async def test_503_does_not_trigger_low_quota(monkeypatch):
    monkeypatch.setattr(config, "RATE_LIMIT_BASE_DELAY", 0.0)
    monkeypatch.setattr(config, "RATE_LIMIT_MAX_RETRIES", 1)
    monkeypatch.setattr(config, "AUTO_LOW_QUOTA", True)
    quota.set_low_quota(False)

    def handler(request):
        return httpx.Response(503, json={"detail": "down"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        with pytest.raises(httpx.HTTPStatusError):
            await llm._post_with_retry(c, "http://x/test", json={})
    # Only 429 means "quota"; 503 is transient and must not degrade the profile.
    assert quota.is_low_quota() is False
