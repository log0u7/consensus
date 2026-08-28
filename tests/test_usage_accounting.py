"""Tests for usage accounting: cached-token parsing (both transports),
cost fallback via pricing, and the summarize_usage aggregator.
"""

import pytest
from src import llm
from src.models import Usage, summarize_usage

# ---------------------------------------------------------------------------
# OpenAI-compatible usage parsing
# ---------------------------------------------------------------------------


def test_read_usage_openai_prompt_tokens_details():
    data = {
        "usage": {
            "prompt_tokens": 1000,
            "completion_tokens": 50,
            "prompt_tokens_details": {"cached_tokens": 800},
        }
    }
    assert llm._read_usage_openai(data) == (1000, 50, 800, None)


def test_read_usage_openai_cache_read_input_tokens():
    """Some gateways forward Anthropic-style cache fields on /chat/completions."""
    data = {"usage": {"input_tokens": 100, "output_tokens": 10, "cache_read_input_tokens": 60}}
    assert llm._read_usage_openai(data) == (100, 10, 60, None)


def test_read_usage_openai_with_cost():
    data = {"usage": {"prompt_tokens": 10, "completion_tokens": 5, "cost": 0.42}}
    assert llm._read_usage_openai(data) == (10, 5, 0, 0.42)


def test_read_usage_openai_no_usage():
    assert llm._read_usage_openai({}) == (0, 0, 0, None)


# ---------------------------------------------------------------------------
# Anthropic usage parsing
# ---------------------------------------------------------------------------


def test_read_usage_anthropic_with_cache():
    data = {
        "usage": {
            "input_tokens": 200,
            "output_tokens": 20,
            "cache_read_input_tokens": 150,
            "cache_creation_input_tokens": 5,
        }
    }
    assert llm._read_usage_anthropic(data) == (200, 20, 150)


def test_read_usage_anthropic_no_cache_fields():
    assert llm._read_usage_anthropic({"usage": {"input_tokens": 1, "output_tokens": 2}}) == (
        1,
        2,
        0,
    )
    assert llm._read_usage_anthropic({}) == (0, 0, 0)


# ---------------------------------------------------------------------------
# _record: sink + pricing fallback
# ---------------------------------------------------------------------------


def test_record_includes_cached_tokens_and_fallback_cost():
    import asyncio

    async def main():
        with llm.usage_scope() as sink:
            llm.set_step("coder")
            llm._record("local", "qwen3-8b", 100, 10, None, 5, cached_tokens=42)
        return sink

    sink = asyncio.run(main())
    (u,) = sink
    assert u.step == "coder"
    assert u.cached_tokens == 42
    assert u.cost == 0.0  # local: pricing says free


def test_record_without_sink_is_noop():
    # Must not raise with no active usage_scope.
    llm._record("zen", "big-pickle", 10, 5, 0.01, 3)


# ---------------------------------------------------------------------------
# Anthropic payload: system as cache_control blocks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_anthropic_system_uses_cache_control(monkeypatch):
    """The system prompt must be sent as a cacheable content block."""
    import httpx
    from src import config
    from src.config import Provider

    captured: dict = {}

    def handler(request):
        captured["payload"] = request.read()
        return httpx.Response(
            200,
            json={
                "content": [{"type": "text", "text": "ok"}],
                "usage": {"input_tokens": 10, "output_tokens": 2, "cache_read_input_tokens": 7},
            },
        )

    fake = Provider(
        name="anthropic",
        base_url="http://mock-anthropic",
        transport="anthropic",
        auth_header="x-api-key",
        auth_value="k",
        extra_headers={"anthropic-version": "2023-06-01"},
    )
    monkeypatch.setattr(config, "get_provider", lambda name: fake)
    monkeypatch.setattr(
        llm,
        "_client",
        lambda p: httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url=p.base_url),
    )

    result = await llm.call_anthropic(
        "claude-sonnet-4", "hi", system="You are stable.", max_tokens=8
    )
    assert result == "ok"

    import json as _json

    payload = _json.loads(captured["payload"])
    assert isinstance(payload["system"], list)
    assert payload["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert payload["system"][0]["text"] == "You are stable."

    # usage recorded through the mock: verify via a fresh scoped call
    async def scoped():
        with llm.usage_scope() as sink2:
            await llm.call_anthropic("claude-sonnet-4", "hi", system="s", max_tokens=8)
        return sink2

    sink = await scoped()
    (u,) = sink
    assert u.cached_tokens == 7
    assert u.input_tokens == 10


@pytest.mark.asyncio
async def test_anthropic_stream_reads_cached_tokens(monkeypatch):
    """message_start usage cache_read_input_tokens must reach _record."""
    import httpx
    from src import config
    from src.config import Provider

    sse = "\n\n".join(
        [
            'data: {"type": "message_start", "message": {"usage": {"input_tokens": 50, "cache_read_input_tokens": 40}}}',
            'data: {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "he"}}',
            'data: {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "llo"}}',
            'data: {"type": "message_delta", "usage": {"output_tokens": 3}}',
            'data: {"type": "message_stop"}',
        ]
    )

    def handler(request):
        return httpx.Response(200, text=sse, headers={"content-type": "text/event-stream"})

    fake = Provider(
        name="anthropic",
        base_url="http://mock-anthropic",
        transport="anthropic",
        auth_header="x-api-key",
        auth_value="k",
        extra_headers={"anthropic-version": "2023-06-01"},
    )
    monkeypatch.setattr(config, "get_provider", lambda name: fake)
    monkeypatch.setattr(
        llm,
        "_client",
        lambda p: httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url=p.base_url),
    )

    async def scoped():
        with llm.usage_scope() as sink:
            parts = [
                d
                async for d in llm.call_anthropic_history_stream(
                    "claude-sonnet-4", [{"role": "user", "content": "hi"}]
                )
            ]
        return parts, sink

    parts, sink = await scoped()
    assert "".join(parts) == "hello"
    (u,) = sink
    assert u.input_tokens == 50
    assert u.cached_tokens == 40
    assert u.output_tokens == 3


# ---------------------------------------------------------------------------
# summarize_usage aggregator
# ---------------------------------------------------------------------------


def test_summarize_usage_empty():
    cs = summarize_usage([])
    assert cs.calls == 0
    assert cs.cost == 0.0
    assert cs.cost_known is False
    assert cs.cached_tokens == 0
    assert cs.by_provider == []


def test_summarize_usage_aggregates_cached_and_by_provider():
    usages = [
        Usage(
            step="coder",
            transport="zen",
            model="big-pickle",
            input_tokens=100,
            output_tokens=10,
            cached_tokens=50,
            cost=0.1,
        ),
        Usage(
            step="reviewer:a",
            transport="zen",
            model="big-pickle",
            input_tokens=50,
            output_tokens=5,
            cached_tokens=20,
            cost=0.2,
        ),
        Usage(
            step="chat",
            transport="local",
            model="qwen3-8b",
            input_tokens=30,
            output_tokens=3,
            cached_tokens=0,
            cost=0.0,
        ),
        Usage(
            step="lead",
            transport="anthropic",
            model="claude",
            input_tokens=10,
            output_tokens=1,
            cached_tokens=0,
            cost=None,
        ),
    ]
    cs = summarize_usage(usages)
    assert cs.calls == 4
    assert cs.input_tokens == 190
    assert cs.output_tokens == 19
    assert cs.cached_tokens == 70
    assert cs.cost == pytest.approx(0.3)
    assert cs.cost_known is True

    by = {p.provider: p for p in cs.by_provider}
    assert set(by) == {"anthropic", "local", "zen"}
    assert by["zen"].calls == 2
    assert by["zen"].cached_tokens == 70
    assert by["zen"].cost == pytest.approx(0.3)
    assert by["zen"].cost_known is True
    assert by["local"].cost_known is True  # local reports 0.0 (known free)
    assert by["anthropic"].cost == 0.0
    assert by["anthropic"].cost_known is False


def test_summarize_usage_all_unknown_costs():
    usages = [
        Usage(step="s", transport="zen", model="m", input_tokens=1, output_tokens=1, cost=None)
    ]
    cs = summarize_usage(usages)
    assert cs.cost_known is False
    assert cs.cost == 0.0
    assert cs.by_provider[0].cost_known is False
