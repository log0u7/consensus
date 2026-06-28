"""Tests for provider config and the openai-compatible transport."""

import httpx
import pytest
from src import config, llm


def test_get_provider_zen_configured():
    """ZEN_API_KEY=dummy is set in conftest so zen must be registered."""
    p = config.get_provider("zen")
    assert p.transport == "openai-compatible"
    assert p.auth_header == "Authorization"
    assert p.auth_value.startswith("Bearer ")


def test_get_provider_unknown_raises():
    with pytest.raises(KeyError, match="not configured"):
        config.get_provider("does_not_exist_xyz")


def test_client_sets_auth_header():
    """_client() must inject the provider's auth header (DRY check)."""
    p = config.get_provider("zen")
    client = llm._client(p)
    assert client.headers.get(p.auth_header) == p.auth_value
    # No x-api-key header on openai-compatible providers
    assert "x-api-key" not in client.headers


@pytest.mark.asyncio
async def test_call_openai_compatible_parses_response(monkeypatch):
    """call_openai_compatible extracts the content from choices[0].message."""
    mock_response = {
        "choices": [{"message": {"role": "assistant", "content": "hello world"}}],
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }

    def handler(request):
        return httpx.Response(200, json=mock_response)

    # Patch _client to return a fresh mock client on each call (never pre-opened)
    monkeypatch.setattr(
        llm,
        "_client",
        lambda p: httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="http://mock-zen"
        ),
    )
    result = await llm.call_openai_compatible("zen", "some-model", "hi")
    assert result == "hello world"


def test_read_usage_openai_standard_fields():
    data = {"usage": {"prompt_tokens": 100, "completion_tokens": 50}}
    inp, out, cost = llm._read_usage_openai(data)
    assert inp == 100
    assert out == 50
    assert cost is None


def test_read_usage_openai_zen_fields():
    """Zen may use input_tokens/output_tokens instead of prompt/completion."""
    data = {"usage": {"input_tokens": 200, "output_tokens": 80, "cost": 0.001}}
    inp, out, cost = llm._read_usage_openai(data)
    assert inp == 200
    assert out == 80
    assert cost == pytest.approx(0.001)


def test_read_usage_openai_cost_details():
    """upstream_inference_cost takes priority over top-level cost."""
    data = {
        "usage": {
            "input_tokens": 10,
            "output_tokens": 5,
            "cost": 0.0,
            "cost_details": {"upstream_inference_cost": 0.0042},
        }
    }
    _, _, cost = llm._read_usage_openai(data)
    assert cost == pytest.approx(0.0042)
