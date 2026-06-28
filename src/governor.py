"""Request governor: rate-limit (aiolimiter) + retry (tenacity) + fallback.

Each provider gets its own AsyncLimiter (RPM cap) so the app never
self-throttles. On 429 / transient errors tenacity retries with exponential
backoff + jitter. If all retries are exhausted, the next provider in the
fallback chain is tried (e.g. zen -> local when the monthly quota is dead).

Usage:
    result = await governor.call(
        provider_name="zen",
        model="deepseek-r1-0528",
        make_call=lambda: llm.complete("zen", "deepseek-r1-0528", user, system, max_tokens),
        fallback=["local"],
    )
"""

import logging
from collections.abc import Awaitable, Callable

import httpx

from . import config

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Rate limiters: one AsyncLimiter per provider, created lazily.
# ---------------------------------------------------------------------------

try:
    from aiolimiter import AsyncLimiter
    _LIMITERS: dict[str, AsyncLimiter] = {}

    def _limiter(provider_name: str) -> AsyncLimiter:
        if provider_name not in _LIMITERS:
            rpm = config.provider_rpm(provider_name)
            # 0 means unlimited: use a very high cap
            _LIMITERS[provider_name] = AsyncLimiter(max(rpm, 10000), 60)
        return _LIMITERS[provider_name]

    _HAS_LIMITER = True
except ImportError:
    log.warning("aiolimiter not installed; rate limiting disabled")
    _HAS_LIMITER = False

    async def _noop_acquire():  # type: ignore[misc]
        pass

    class _FakeLimiter:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *_):
            pass

    def _limiter(provider_name: str) -> "_FakeLimiter":  # type: ignore[misc]
        return _FakeLimiter()


# ---------------------------------------------------------------------------
# Retry predicate + tenacity setup
# ---------------------------------------------------------------------------

def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in config.RATE_LIMIT_RETRY_STATUSES
    return isinstance(exc, (httpx.ConnectError, httpx.ReadTimeout, httpx.RemoteProtocolError))


try:
    from tenacity import (
        AsyncRetrying,
        retry_if_exception,
        stop_after_attempt,
        wait_exponential_jitter,
    )
    _HAS_TENACITY = True
except ImportError:
    log.warning("tenacity not installed; retry logic falls back to built-in")
    _HAS_TENACITY = False


# ---------------------------------------------------------------------------
# Core call with rate-limit + retry
# ---------------------------------------------------------------------------

async def _call_once(provider_name: str, make_call: Callable[[], Awaitable[str]]) -> str:
    """Acquire a rate-limit slot then execute make_call with tenacity retry."""
    async with _limiter(provider_name):
        if _HAS_TENACITY:
            async for attempt in AsyncRetrying(
                retry=retry_if_exception(_is_retryable),
                wait=wait_exponential_jitter(
                    initial=config.RATE_LIMIT_BASE_DELAY,
                    max=config.RATE_LIMIT_MAX_DELAY,
                ),
                stop=stop_after_attempt(config.RATE_LIMIT_MAX_RETRIES + 1),
                reraise=True,
            ):
                with attempt:
                    return await make_call()
        else:
            return await make_call()
    # unreachable but satisfies type checkers
    raise RuntimeError("governor._call_once: unreachable")  # pragma: no cover


async def call(
    provider_name: str,
    make_call: Callable[[], Awaitable[str]],
    fallback: list[str] | None = None,
    fallback_factory: "Callable[[str], Callable[[], Awaitable[str]]] | None" = None,
) -> str:
    """Try provider_name, then each provider in fallback[], in order.

    fallback_factory(provider_name) builds the make_call for the fallback
    provider (needed because the call must reference the new provider).
    If fallback_factory is None the same make_call is retried on the next
    provider (works only when make_call does not embed a provider name).
    """
    chain = [provider_name] + (fallback or [])
    last_exc: Exception | None = None
    for pname in chain:
        try:
            call_fn = (
                fallback_factory(pname)
                if fallback_factory and pname != provider_name
                else make_call
            )
            return await _call_once(pname, call_fn)
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "governor: provider %r exhausted retries (%s: %s); trying next",
                pname, type(exc).__name__, exc,
            )
            last_exc = exc  # type: ignore[assignment]
            if pname == provider_name:
                from . import quota
                if not quota.is_low_quota() and config.AUTO_LOW_QUOTA:
                    quota.set_low_quota(True)
                    log.warning("governor: auto-enabling low-quota mode")
    raise last_exc or RuntimeError("governor: all providers exhausted")
