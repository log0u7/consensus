"""Tests for src/skills.py, src/mcp_client.py, and src/context.py."""

import pytest
from src import skills as skills_mod
from src.context import AgentContext, build
from src.roles import Role

# ---------------------------------------------------------------------------
# skills.py
# ---------------------------------------------------------------------------

def test_list_available_includes_bundled():
    available = skills_mod.list_available()
    assert "coding" in available
    assert "review" in available
    assert "sre" in available
    assert "pentest" in available


def test_load_known_skill():
    content = skills_mod.load("coding")
    assert len(content) > 0
    assert "code" in content.lower()


def test_load_unknown_skill_returns_empty():
    content = skills_mod.load("does_not_exist_xyz")
    assert content == ""


def test_load_skills_concatenates():
    content = skills_mod.load_skills(["coding", "review"])
    assert "## Skill: coding" in content
    assert "## Skill: review" in content


def test_load_skills_skips_missing():
    content = skills_mod.load_skills(["coding", "totally_missing"])
    assert "coding" in content
    assert "totally_missing" not in content


def test_load_skills_empty_list():
    assert skills_mod.load_skills([]) == ""


# ---------------------------------------------------------------------------
# context.py
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_build_injects_skills():
    role = Role(name="coder", model="zen/deepseek-v3-0324", skills=["coding"])
    ctx = await build("write a hello world", role)
    assert isinstance(ctx, AgentContext)
    assert "coding" in ctx.system.lower() or "Skill" in ctx.system


@pytest.mark.asyncio
async def test_build_no_skills():
    role = Role(name="coder", model="zen/deepseek-v3-0324")
    ctx = await build("write hello world", role)
    assert "write hello world" in ctx.user


@pytest.mark.asyncio
async def test_build_with_tools():
    role = Role(name="coder", model="zen/deepseek-v3-0324")
    tools = [{"name": "read_file", "description": "Read a file", "input_schema": {}}]
    ctx = await build("spec", role, tool_definitions=tools)
    assert "read_file" in ctx.system


@pytest.mark.asyncio
async def test_build_with_rag_hits():
    role = Role(name="coder", model="zen/deepseek-v3-0324")
    hits = [{"source": "docs/api.md", "chunk_idx": 0, "content": "API reference"}]
    ctx = await build("spec", role, rag_hits=hits)
    assert "API reference" in ctx.user
    assert ctx.rag_sources == hits


@pytest.mark.asyncio
async def test_build_stable_prefix_order():
    """System must come before user; skills before RAG in system."""
    role = Role(name="coder", model="zen/deepseek-v3-0324", skills=["coding"])
    ctx = await build(
        "volatile spec",
        role,
        base_system="BASE SYSTEM",
        rag_hits=[{"source": "s", "chunk_idx": 0, "content": "rag content"}],
    )
    # Skills in system, spec in user, rag in user.
    assert "BASE SYSTEM" in ctx.system
    assert "volatile spec" in ctx.user
    assert "rag content" in ctx.user


def test_tokens_estimate():
    ctx = AgentContext(system="a" * 400, user="b" * 400)
    # 800 chars / 4 = 200 tokens
    assert ctx.tokens_estimate == 200


# ---------------------------------------------------------------------------
# mcp_client.py: import error when sdk is missing
# ---------------------------------------------------------------------------

def test_mcp_client_raises_on_missing_sdk(monkeypatch):
    import builtins
    real_import = builtins.__import__

    def mock_import(name, *args, **kwargs):
        if name == "mcp":
            raise ImportError("mcp not installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", mock_import)

    import asyncio

    from src.mcp_client import MCPClientManager

    async def _run():
        async with MCPClientManager([{"name": "test", "transport": "stdio", "command": ["echo"]}]):
            pass

    with pytest.raises(ImportError, match="mcp"):
        asyncio.run(_run())


# ---------------------------------------------------------------------------
# New teams: SRE and pentest YAML load correctly
# ---------------------------------------------------------------------------

def test_load_sre_team():
    from src import roles as roles_mod
    team = roles_mod.load("sre")
    assert team.topology == "pipeline"
    assert "planner" in team.roles
    assert "executor" in team.roles
    assert "verifier" in team.roles
    assert team.roles["planner"].skills == ["sre"]


def test_load_pentest_team():
    from src import roles as roles_mod
    team = roles_mod.load("pentest")
    assert team.topology == "loop"
    assert team.sandbox is True
    assert "recon" in team.roles
    assert "exploit" in team.roles
    assert team.roles["exploit"].sandbox is True
