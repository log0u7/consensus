"""Thin async LLM transports over any OpenAI-compatible or Anthropic provider.

No SDK, no agent framework: just httpx.  One _client() factory per provider
ensures auth headers and TLS settings are set exactly once (DRY).

Two transports are supported:
  openai-compatible  POST /chat/completions  (Zen, OpenAI, local Ollama/vLLM, ...)
  anthropic          POST /messages          (Anthropic Messages API, native SSE)

Usage accounting is collected via usage_scope() / _record() using contextvars
so asyncio.gather (parallel panel) accumulates correctly.
"""

import asyncio
import contextvars
import email.utils
import json
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import httpx

from . import config
from .models import Usage

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# HTTP client factory (DRY: single place for auth, TLS, timeout)
# ---------------------------------------------------------------------------

def _client(provider: config.Provider) -> httpx.AsyncClient:
    """Return a configured async client for the given provider."""
    return httpx.AsyncClient(
        base_url=provider.base_url,
        verify=provider.verify_tls,
        headers={
            provider.auth_header: provider.auth_value,
            **provider.extra_headers,
        },
        timeout=httpx.Timeout(config.HTTP_TIMEOUT, connect=10.0),
    )


# ---------------------------------------------------------------------------
# Retry helper (rate-limit / transient errors)
# Exponential backoff with jitter, honouring Retry-After.
# The governor (governor.py) adds aiolimiter + tenacity on top; this is the
# low-level transport retry kept here for simplicity when calling outside a
# governed context (e.g. streaming chat).
# ---------------------------------------------------------------------------

def _parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    value = value.strip()
    try:
        return float(value)
    except ValueError:
        pass
    try:
        dt = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if dt is None:
        return None
    import datetime as _dt
    now = _dt.datetime.now(dt.tzinfo) if dt.tzinfo else _dt.datetime.now()
    return max(0.0, (dt - now).total_seconds())


def _backoff_delay(attempt: int, retry_after: float | None) -> float:
    import random
    if retry_after is not None:
        return min(retry_after, config.RATE_LIMIT_MAX_DELAY)
    base = config.RATE_LIMIT_BASE_DELAY * (2 ** attempt)
    return min(base, config.RATE_LIMIT_MAX_DELAY) * (0.5 + random.random() / 2)


async def _post_with_retry(
    client: httpx.AsyncClient, path: str, **kwargs
) -> httpx.Response:
    """POST with retry on rate-limit/transient statuses."""
    attempt = 0
    while True:
        r = await client.post(path, **kwargs)
        if (
            r.status_code in config.RATE_LIMIT_RETRY_STATUSES
            and attempt < config.RATE_LIMIT_MAX_RETRIES
        ):
            delay = _backoff_delay(
                attempt, _parse_retry_after(r.headers.get("Retry-After"))
            )
            log.warning(
                "provider %s on %s; retry in %.1fs (attempt %d/%d)",
                r.status_code, path, delay, attempt + 1, config.RATE_LIMIT_MAX_RETRIES,
            )
            await asyncio.sleep(delay)
            attempt += 1
            continue
        if r.status_code in config.RATE_LIMIT_RETRY_STATUSES:
            _maybe_auto_low_quota(r.status_code)
        r.raise_for_status()
        return r


def _maybe_auto_low_quota(status: int) -> None:
    if not config.AUTO_LOW_QUOTA or status != 429:
        return
    from . import quota
    if not quota.is_low_quota():
        quota.set_low_quota(True)
        log.warning("429 retries exhausted; auto-enabling low-quota mode")


# ---------------------------------------------------------------------------
# Usage accumulator (contextvars - correct under asyncio.gather)
# ---------------------------------------------------------------------------

_usage_sink: contextvars.ContextVar[list[Usage] | None] = contextvars.ContextVar(
    "usage_sink", default=None
)
_current_step: contextvars.ContextVar[str] = contextvars.ContextVar(
    "current_step", default=""
)


class usage_scope:
    """Context manager that collects every LLM call's Usage made within it."""

    def __enter__(self) -> list[Usage]:
        self._sink: list[Usage] = []
        self._token = _usage_sink.set(self._sink)
        return self._sink

    def __exit__(self, *exc) -> None:
        _usage_sink.reset(self._token)


def set_step(step: str) -> contextvars.Token:
    return _current_step.set(step)


def reset_step(token: contextvars.Token) -> None:
    _current_step.reset(token)


def _record(
    provider: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cost: float | None,
    latency_ms: int,
) -> None:
    sink = _usage_sink.get()
    if sink is None:
        return
    sink.append(
        Usage(
            step=_current_step.get(),
            transport=provider,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost=cost,
            latency_ms=latency_ms,
        )
    )


def _read_usage_openai(data: dict) -> tuple[int, int, float | None]:
    """Extract (input, output, cost) from an OpenAI-compatible response."""
    u = data.get("usage") or {}
    # Some providers (e.g. Zen, OpenRouter BYOK) bury real cost in cost_details.
    details = u.get("cost_details") or {}
    cost = details.get("upstream_inference_cost") or u.get("cost")
    return (
        int(u.get("input_tokens", u.get("prompt_tokens", 0))),
        int(u.get("output_tokens", u.get("completion_tokens", 0))),
        float(cost) if cost is not None else None,
    )


def _read_usage_anthropic(data: dict) -> tuple[int, int]:
    u = data.get("usage") or {}
    return int(u.get("input_tokens", 0)), int(u.get("output_tokens", 0))


# ---------------------------------------------------------------------------
# Health probe (no billable call)
# ---------------------------------------------------------------------------

async def provider_reachable(provider_name: str, timeout: float = 5.0) -> dict:
    """Lightweight reachability probe for the /health or /models endpoint."""
    try:
        provider = config.get_provider(provider_name)
        async with httpx.AsyncClient(
            base_url=provider.base_url,
            verify=provider.verify_tls,
            headers={provider.auth_header: provider.auth_value, **provider.extra_headers},
            timeout=httpx.Timeout(timeout, connect=timeout),
        ) as c:
            r = await c.get("/models")
            return {"reachable": True, "status": r.status_code, "error": None}
    except Exception as exc:  # noqa: BLE001
        return {"reachable": False, "status": None, "error": f"{type(exc).__name__}: {exc}"}


# ---------------------------------------------------------------------------
# OpenAI-compatible transport  (Zen, OpenAI, local, ...)
# POST /chat/completions
# ---------------------------------------------------------------------------

async def call_openai_compatible(
    provider_name: str,
    model: str,
    user: str,
    system: str | None = None,
    max_tokens: int = 4096,
) -> str:
    """Single-turn completion via POST /chat/completions."""
    messages: list[dict[str, str]] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": user})
    return await call_openai_compatible_history(provider_name, model, messages, max_tokens)


async def call_openai_compatible_history(
    provider_name: str,
    model: str,
    messages: list[dict[str, str]],
    max_tokens: int = 4096,
) -> str:
    """Multi-turn completion via POST /chat/completions (full message history)."""
    from . import cache as cache_mod
    cached = cache_mod.get(messages, model)
    if cached is not None:
        return cached

    provider = config.get_provider(provider_name)
    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "max_completion_tokens": max_tokens,
    }
    t0 = time.perf_counter()
    async with _client(provider) as c:
        r = await _post_with_retry(c, "/chat/completions", json=payload)
        data = r.json()
    inp, out, cost = _read_usage_openai(data)
    _record(provider_name, model, inp, out, cost, int((time.perf_counter() - t0) * 1000))
    result = data["choices"][0]["message"]["content"]
    cache_mod.put(messages, model, result)
    return result


# ---------------------------------------------------------------------------
# Anthropic Messages transport  (native, supports SSE streaming)
# POST /messages
# ---------------------------------------------------------------------------

async def call_anthropic(
    model: str,
    user: str,
    system: str | None = None,
    max_tokens: int = 4096,
) -> str:
    """Single-turn completion via Anthropic Messages API."""
    return await _call_anthropic_messages(model, [{"role": "user", "content": user}], system, max_tokens)


async def call_anthropic_history(
    model: str,
    messages: list[dict[str, str]],
    system: str | None = None,
    max_tokens: int = 4096,
) -> str:
    """Multi-turn completion via Anthropic Messages API."""
    return await _call_anthropic_messages(model, messages, system, max_tokens)


async def _call_anthropic_messages(
    model: str,
    messages: list[dict[str, str]],
    system: str | None,
    max_tokens: int,
) -> str:
    from . import cache as cache_mod
    cache_key_msgs = [{"role": "system", "content": system or ""}, *messages]
    cached = cache_mod.get(cache_key_msgs, model)
    if cached is not None:
        return cached

    provider = config.get_provider("anthropic")
    payload: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": messages,
    }
    if system:
        payload["system"] = system
    t0 = time.perf_counter()
    async with _client(provider) as c:
        r = await _post_with_retry(c, "/messages", json=payload)
        data = r.json()
    inp, out = _read_usage_anthropic(data)
    _record("anthropic", model, inp, out, None, int((time.perf_counter() - t0) * 1000))
    result = "".join(
        b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"
    )
    cache_mod.put(cache_key_msgs, model, result)
    return result


async def call_anthropic_history_stream(
    model: str,
    messages: list[dict[str, str]],
    system: str | None = None,
    max_tokens: int = 4096,
) -> AsyncIterator[str]:
    """Stream the Anthropic Messages SSE, yielding text deltas."""
    provider = config.get_provider("anthropic")
    payload: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": messages,
        "stream": True,
    }
    if system:
        payload["system"] = system

    t0 = time.perf_counter()
    input_tokens = output_tokens = 0
    async with _client(provider) as c:
        attempt = 0
        while True:
            async with c.stream("POST", "/messages", json=payload) as r:
                if (
                    r.status_code in config.RATE_LIMIT_RETRY_STATUSES
                    and attempt < config.RATE_LIMIT_MAX_RETRIES
                ):
                    delay = _backoff_delay(
                        attempt, _parse_retry_after(r.headers.get("Retry-After"))
                    )
                    log.warning(
                        "provider %s on stream; retry in %.1fs (attempt %d/%d)",
                        r.status_code, delay, attempt + 1, config.RATE_LIMIT_MAX_RETRIES,
                    )
                    await r.aclose()
                    await asyncio.sleep(delay)
                    attempt += 1
                    continue
                if r.status_code in config.RATE_LIMIT_RETRY_STATUSES:
                    _maybe_auto_low_quota(r.status_code)
                r.raise_for_status()
                async for line in r.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    raw = line[len("data:"):].strip()
                    if not raw:
                        continue
                    try:
                        event = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    etype = event.get("type")
                    if etype == "message_start":
                        u = (event.get("message") or {}).get("usage") or {}
                        input_tokens = int(u.get("input_tokens", 0))
                    elif etype == "content_block_delta":
                        delta = event.get("delta") or {}
                        if delta.get("type") == "text_delta":
                            text = delta.get("text", "")
                            if text:
                                yield text
                    elif etype == "message_delta":
                        u = event.get("usage") or {}
                        output_tokens = int(u.get("output_tokens", output_tokens))
            break
    _record(
        "anthropic", model, input_tokens, output_tokens, None,
        int((time.perf_counter() - t0) * 1000),
    )


# ---------------------------------------------------------------------------
# Transport dispatch  (KISS: simple if/elif over 2 wire formats)
# ---------------------------------------------------------------------------

async def complete(
    provider_name: str,
    model: str,
    user: str,
    system: str | None = None,
    max_tokens: int = 4096,
) -> str:
    """Route a single-turn completion to the right transport."""
    provider = config.get_provider(provider_name)
    if provider.transport == "openai-compatible":
        return await call_openai_compatible(provider_name, model, user, system, max_tokens)
    if provider.transport == "anthropic":
        return await call_anthropic(model, user, system, max_tokens)
    raise ValueError(f"unknown transport for provider {provider_name!r}: {provider.transport}")


async def complete_history(
    provider_name: str,
    model: str,
    messages: list[dict[str, str]],
    max_tokens: int = 4096,
) -> str:
    """Route a multi-turn completion (full message history) to the right transport."""
    provider = config.get_provider(provider_name)
    if provider.transport == "openai-compatible":
        return await call_openai_compatible_history(provider_name, model, messages, max_tokens)
    if provider.transport == "anthropic":
        return await call_anthropic_history(model, messages, max_tokens=max_tokens)
    raise ValueError(f"unknown transport for provider {provider_name!r}: {provider.transport}")


# ---------------------------------------------------------------------------
# JSON helpers
# ---------------------------------------------------------------------------

_DECODER = json.JSONDecoder()

_JSON_RETRY_HINT = (
    "\n\nYour previous answer was not valid JSON. Return ONLY a single valid "
    "JSON value, with no prose and no markdown fences."
)


def parse_json(text: str) -> "dict | list | None":
    """Best-effort JSON extraction.  Handles markdown fences and trailing prose
    by raw_decode from the first '{' or '['.  Returns None on failure."""
    try:
        return json.loads(text.strip())
    except json.JSONDecodeError:
        pass
    text = text.strip()
    candidates = [i for i in (text.find("{"), text.find("[")) if i != -1]
    if not candidates:
        return None
    start = min(candidates)
    try:
        value, _ = _DECODER.raw_decode(text[start:])
        if isinstance(value, (dict, list)):
            return value
    except json.JSONDecodeError:
        pass

    # Last resort: try json-repair if available
    try:
        from json_repair import repair_json  # type: ignore[import-untyped]
        repaired = repair_json(text)
        if repaired:
            parsed = json.loads(repaired)
            if isinstance(parsed, (dict, list)):
                return parsed
    except Exception:  # noqa: BLE001
        pass
    return None


async def complete_json(
    make_call: Callable[[int], Awaitable[str]],
    retries: int = 2,
) -> "dict | list":
    """Call a coroutine factory, parse JSON, retry on failure."""
    last = ""
    for attempt in range(retries + 1):
        last = await make_call(attempt)
        parsed = parse_json(last)
        if parsed is not None:
            return parsed
    raise ValueError(f"could not parse JSON, last answer: {last[:300]}")


async def complete_json_obj(
    make_call: Callable[[int], Awaitable[str]],
    retries: int = 2,
) -> dict:
    """Like complete_json but asserts the result is a JSON object."""
    parsed = await complete_json(make_call, retries=retries)
    if not isinstance(parsed, dict):
        raise ValueError("expected a JSON object, got a non-object value")
    return parsed
