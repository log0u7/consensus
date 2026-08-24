"""Security tests for src/mcp_client.py.

Covers:
  - _validate_mcp_url: https mandatory for remote hosts, http only loopback
  - stdio spawn visibility (audit log) is emitted before connecting
"""

import logging

import pytest
from src.mcp_client import MCPClientManager, _validate_mcp_url

# ---------------------------------------------------------------------------
# URL validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://tools.example.com/mcp",
        "https://mcp.internal:8443/",
        "http://localhost:8080/mcp",
        "http://127.0.0.1:9000",
    ],
)
def test_validate_mcp_url_accepts(url):
    _validate_mcp_url(url)  # must not raise


@pytest.mark.parametrize(
    "url",
    [
        "http://tools.example.com/mcp",
        "http://169.254.169.254/latest/meta-data",  # SSRF-style target
        "ftp://tools.example.com/mcp",
        "",
    ],
)
def test_validate_mcp_url_rejects_remote_cleartext(url):
    with pytest.raises(ValueError):
        _validate_mcp_url(url)


# ---------------------------------------------------------------------------
# stdio audit log
# ---------------------------------------------------------------------------


class _FakeSession:
    def __init__(self, *a, **kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def initialize(self):
        return None

    async def list_tools(self):
        class _Resp:
            tools = []

        return _Resp()


@pytest.mark.asyncio
async def test_stdio_command_is_logged(monkeypatch, caplog):
    """Manifests spawn local processes by design; every command is auditable."""
    pytest.importorskip("mcp")
    import mcp.client.stdio as stdio_mod

    monkeypatch.setattr(stdio_mod, "stdio_client", lambda params: _FakeSession())
    monkeypatch.setattr("mcp.ClientSession", _FakeSession)

    mgr = MCPClientManager(
        [{"name": "local-tools", "transport": "stdio", "command": ["uvx", "some-server"]}]
    )
    with caplog.at_level(logging.INFO, logger="src.mcp_client"):
        await mgr.__aenter__()

    assert any("uvx" in rec.getMessage() for rec in caplog.records)
    await mgr.__aexit__(None, None, None)


@pytest.mark.asyncio
async def test_http_remote_cleartext_fails_loud():
    """A misconfigured manifest must raise, not connect in cleartext."""
    pytest.importorskip("mcp")
    mgr = MCPClientManager([{"name": "bad", "transport": "http", "url": "http://evil.example.com"}])
    with pytest.raises(ValueError, match="https"):
        await mgr.__aenter__()
