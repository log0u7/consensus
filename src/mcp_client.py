"""MCP client manager: connect to N MCP servers and aggregate their tools.

Supports two transports:
  stdio    local processes (e.g. Serena LSP, custom tool servers)
  http     remote MCP services (Streamable HTTP, the current MCP standard)

Usage:
    async with MCPClientManager(servers_config) as mgr:
        tools = await mgr.list_tools()
        result = await mgr.call_tool("read_file", {"path": "src/main.py"})

Server configuration (list of dicts, typically from a team YAML or env):
  - name: serena
    transport: stdio
    command: ["uvx", "--from", "git+https://github.com/oraios/serena",
              "serena", "start-mcp-server", "--context", "ide-assistant",
              "--project", "."]
  - name: my-remote-tool
    transport: http
    url: https://tools.example.com/mcp

The MCP SDK (modelcontextprotocol/python-sdk) is a soft dependency: if it
is not installed, MCPClientManager raises ImportError with a clear message.
The rest of the application works without it (YAGNI: only loaded when tools
are referenced in a team manifest).
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)


class MCPClientManager:
    """Async context manager: connects to N MCP servers, aggregates tools."""

    def __init__(self, servers: list[dict]) -> None:
        self._server_configs = servers
        self._sessions: list[Any] = []
        self._tool_index: dict[str, Any] = {}  # tool_name -> session

    async def __aenter__(self) -> MCPClientManager:
        try:
            from mcp import ClientSession, StdioServerParameters  # type: ignore[import-untyped]
            from mcp.client.stdio import stdio_client  # type: ignore[import-untyped]
        except ImportError as exc:
            raise ImportError(
                "The 'mcp' package is required to use MCP tools. "
                "Install it with: pip install mcp"
            ) from exc

        for cfg in self._server_configs:
            transport = cfg.get("transport", "stdio")
            name = cfg.get("name", "unknown")
            try:
                if transport == "stdio":
                    cmd = cfg["command"]
                    params = StdioServerParameters(command=cmd[0], args=cmd[1:])
                    read, write = await stdio_client(params).__aenter__()
                    session = ClientSession(read, write)
                    await session.__aenter__()
                    await session.initialize()
                    self._sessions.append(session)
                    # Index tools from this server.
                    tools_resp = await session.list_tools()
                    for tool in tools_resp.tools:
                        self._tool_index[tool.name] = session
                        log.debug("MCP tool registered: %s (server=%s)", tool.name, name)
                elif transport == "http":
                    # Streamable HTTP transport (MCP 1.x standard).
                    try:
                        from mcp.client.streamable_http import (
                            streamablehttp_client,  # type: ignore[import-untyped]
                        )
                    except ImportError as exc:
                        raise ImportError(
                            "Streamable HTTP MCP transport requires mcp>=1.3. "
                            "Upgrade with: pip install 'mcp>=1.3'"
                        ) from exc
                    url = cfg["url"]
                    read, write, _ = await streamablehttp_client(url).__aenter__()
                    session = ClientSession(read, write)
                    await session.__aenter__()
                    await session.initialize()
                    self._sessions.append(session)
                    tools_resp = await session.list_tools()
                    for tool in tools_resp.tools:
                        self._tool_index[tool.name] = session
                        log.debug("MCP tool registered: %s (server=%s url=%s)", tool.name, name, url)
                else:
                    log.warning("MCP server %r: unknown transport %r, skipping", name, transport)
            except Exception as exc:  # noqa: BLE001 - resilient: skip failing servers
                log.warning("MCP server %r failed to connect: %s", name, exc)

        return self

    async def __aexit__(self, *exc) -> None:
        for session in self._sessions:
            try:
                await session.__aexit__(*exc)
            except Exception:  # noqa: BLE001
                pass

    async def list_tools(self) -> list[dict]:
        """Return all available tools as a list of {name, description, schema} dicts."""
        result = []
        for name, session in self._tool_index.items():
            tools_resp = await session.list_tools()
            for t in tools_resp.tools:
                if t.name == name:
                    result.append({
                        "name": t.name,
                        "description": getattr(t, "description", ""),
                        "input_schema": getattr(t, "inputSchema", {}),
                    })
        return result

    async def call_tool(self, name: str, arguments: dict) -> str:
        """Call a tool by name and return its result as a string."""
        session = self._tool_index.get(name)
        if session is None:
            raise KeyError(f"MCP tool {name!r} not found. Available: {list(self._tool_index)}")
        result = await session.call_tool(name, arguments)
        # MCP tool results are a list of content blocks; join text blocks.
        parts = []
        for block in result.content:
            if hasattr(block, "text"):
                parts.append(block.text)
        return "\n".join(parts)

    def tool_names(self) -> list[str]:
        return list(self._tool_index)
