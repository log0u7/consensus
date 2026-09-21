"""Tests for the MCP tool loop: ToolRuntime, _tool_loop, agents integration.

The tool loop lets an agent call MCP tools mid-answer: the model returns a
{"tool": name, "arguments": {...}} JSON object, the runtime executes it, the
result is appended to the conversation, and the model is re-prompted until it
produces a final answer (or MCP_MAX_ROUNDS is exhausted).
"""

import json

import pytest
from src.agents import ToolRuntime, _tool_loop


def _runtime(responses: dict[str, str]) -> tuple[ToolRuntime, list[tuple[str, dict]]]:
    calls: list[tuple[str, dict]] = []

    async def _call(name: str, arguments: dict) -> str:
        calls.append((name, arguments))
        return responses[name]

    runtime = ToolRuntime(
        definitions=[{"name": "read_file", "description": "Read a file", "input_schema": {}}],
        call=_call,
    )
    return runtime, calls


@pytest.mark.asyncio
async def test_tool_loop_calls_tool_then_final_answer(monkeypatch):
    rt, calls = _runtime({"read_file": "file contents here"})
    history = [{"role": "user", "content": "read main.py"}]
    rounds: list[list] = []

    async def fake_history(prov, model, messages, max_tokens=4096):
        rounds.append([dict(m) for m in messages])
        if len(rounds) == 1:
            # First answer: the model asks for a tool.
            return json.dumps({"tool": "read_file", "arguments": {"path": "main.py"}})
        return "The file says: file contents here"

    monkeypatch.setattr("src.agents.llm.complete_history", fake_history)

    result = await _tool_loop("zen", "m", rt, history=history)
    assert result == "The file says: file contents here"
    assert calls == [("read_file", {"path": "main.py"})]
    # Round 2 must carry the assistant tool request and the tool result.
    round2 = rounds[1]
    assert any(m["role"] == "tool" and "file contents here" in m["content"] for m in round2)


@pytest.mark.asyncio
async def test_tool_loop_max_rounds_exhausted(monkeypatch):
    rt, calls = _runtime({"read_file": "x"})

    async def fake_history(prov, model, messages, max_tokens=4096):
        return json.dumps({"tool": "read_file", "arguments": {}})

    monkeypatch.setattr("src.agents.llm.complete_history", fake_history)

    with pytest.raises(ValueError, match="rounds"):
        await _tool_loop("zen", "m", rt, max_rounds=2)


@pytest.mark.asyncio
async def test_tool_loop_system_prepended(monkeypatch):
    rt, calls = _runtime({})
    seen: list[list] = []

    async def fake_history(prov, model, messages, max_tokens=4096):
        seen.append(messages)
        return "done"

    monkeypatch.setattr("src.agents.llm.complete_history", fake_history)

    rt.definitions = [{"name": "t", "description": "d", "input_schema": {}}]
    result = await _tool_loop(
        "zen", "m", rt, "You are helpful.", [{"role": "user", "content": "hi"}]
    )
    assert result == "done"
    assert seen[0][0]["role"] == "system"
    assert "You are helpful." in seen[0][0]["content"]
    assert "- t: d" in seen[0][0]["content"]


@pytest.mark.asyncio
async def test_tool_loop_non_json_answer_ends_loop(monkeypatch):
    """A plain-text answer (not a tool call) is returned as final."""
    rt, calls = _runtime({})

    async def fake_history(prov, model, messages, max_tokens=4096):
        return "just text, no json"

    monkeypatch.setattr("src.agents.llm.complete_history", fake_history)
    result = await _tool_loop("zen", "m", rt, "", [{"role": "user", "content": "hi"}])
    assert result == "just text, no json"
    assert calls == []


# ---------------------------------------------------------------------------
# agents integration: write_code / governed_call accept ToolRuntime
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_write_code_with_tools(monkeypatch):
    """write_code(tools=...) runs the tool loop, then parses the final JSON."""
    from src import agents

    rt, calls = _runtime({"read_file": "DATA"})

    async def fake_history(prov, model, messages, max_tokens=4096):
        if len([m for m in messages if m["role"] == "tool"]) == 0:
            return json.dumps({"tool": "read_file", "arguments": {"path": "m.py"}})
        return json.dumps({"language": "python", "code": "print(1)", "notes": "used tools"})

    monkeypatch.setattr("src.agents.llm.complete_history", fake_history)

    result = await agents.write_code(
        "do it",
        provider="zen",
        model="some-model",
        tools=rt,
    )
    assert result["code"] == "print(1)"
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_governed_call_with_tools(monkeypatch):
    from src import agents

    rt, calls = _runtime({"t": "ok"})

    async def fake_history(prov, model, messages, max_tokens=4096):
        return "final text answer"

    monkeypatch.setattr("src.agents.llm.complete_history", fake_history)
    out = await agents.governed_call("zen", "m", "user prompt", tools=rt)
    assert out == "final text answer"


@pytest.mark.asyncio
async def test_tool_call_failure_becomes_data(monkeypatch):
    """A failing tool must not kill the run: the error is fed back as data."""
    from src import agents

    calls: list = []

    async def boom(name, arguments):
        calls.append(name)
        raise RuntimeError("disk on fire")

    rt = ToolRuntime(definitions=[{"name": "t", "description": "", "input_schema": {}}], call=boom)
    seq = {"n": 0}

    async def fake_history(prov, model, messages, max_tokens=4096):
        seq["n"] += 1
        if seq["n"] == 1:
            return json.dumps({"tool": "t", "arguments": {}})
        # Second round: the model sees the TOOL ERROR message and answers.
        tool_msgs = [m for m in messages if m["role"] == "tool"]
        assert tool_msgs and "disk on fire" in tool_msgs[-1]["content"]
        return json.dumps({"language": "python", "code": "x", "notes": ""})

    monkeypatch.setattr("src.agents.llm.complete_history", fake_history)
    result = await agents.write_code("spec", provider="zen", model="m", tools=rt)
    assert result["code"] == "x"
    assert calls == ["t"]
