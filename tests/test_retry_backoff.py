import httpx
import pytest
from src import config, llm


def test_parse_retry_after_seconds():
    assert llm._parse_retry_after("5") == 5.0


def test_parse_retry_after_http_date_future():
    v = llm._parse_retry_after("Wed, 21 Oct 2099 07:28:00 GMT")
    assert v is not None and v > 0


def test_parse_retry_after_garbage():
    assert llm._parse_retry_after("garbage") is None
    assert llm._parse_retry_after(None) is None


def test_backoff_honours_retry_after_and_caps():
    assert llm._backoff_delay(0, 3.0) == min(3.0, config.RATE_LIMIT_MAX_DELAY)
    assert llm._backoff_delay(5, None) <= config.RATE_LIMIT_MAX_DELAY


@pytest.mark.asyncio
async def test_post_with_retry_succeeds_after_429(monkeypatch):
    monkeypatch.setattr(config, "RATE_LIMIT_BASE_DELAY", 0.0)
    monkeypatch.setattr(config, "RATE_LIMIT_MAX_RETRIES", 3)
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(429, headers={"Retry-After": "0"}, json={"detail": "no"})
        return httpx.Response(200, json={"ok": True})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        r = await llm._post_with_retry(c, "http://x/test", json={})
    assert r.status_code == 200
    assert calls["n"] == 3


@pytest.mark.asyncio
async def test_post_with_retry_raises_after_exhaustion(monkeypatch):
    monkeypatch.setattr(config, "RATE_LIMIT_BASE_DELAY", 0.0)
    monkeypatch.setattr(config, "RATE_LIMIT_MAX_RETRIES", 2)
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(429, json={"detail": "no"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        with pytest.raises(httpx.HTTPStatusError):
            await llm._post_with_retry(c, "http://x/test", json={})
    # 1 initial + 2 retries
    assert calls["n"] == 3
