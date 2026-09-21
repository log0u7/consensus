"""Tests for roles/teams YAML loading and topology dispatch.

Parity test: the 'consensus' team must emit exactly the same event types
as the original hardcoded pipeline (code, review*, consensus, result).
"""

from pathlib import Path

import pytest
from src import roles as roles_mod
from src.roles import Role, Team

# ---------------------------------------------------------------------------
# roles.py unit tests
# ---------------------------------------------------------------------------


def test_load_consensus_team():
    team = roles_mod.load("consensus")
    assert team.name == "consensus"
    assert team.topology == "consensus"
    assert "coder" in team.roles
    assert "reviewer" in team.roles
    assert "consensus" in team.roles
    assert "lead" in team.roles


def test_load_consensus_tested_team():
    team = roles_mod.load("consensus-tested")
    assert team.sandbox is True
    coder = team.roles["coder"]
    assert coder.sandbox is True


def test_load_unknown_team_raises():
    with pytest.raises(FileNotFoundError, match="not found"):
        roles_mod.load("does_not_exist_xyz")


def test_list_teams():
    teams = roles_mod.list_teams()
    assert "consensus" in teams
    assert "consensus-tested" in teams


def test_role_defaults():
    r = Role(name="test", model="zen/deepseek-v3-0324")
    assert r.fallback == []
    assert r.skills == []
    assert r.sandbox is False


def test_reviewer_members_parsed(tmp_path, monkeypatch):
    """Explicit 'members' in a team manifest are parsed into panel members."""
    manifest = tmp_path / "t-members.yaml"
    manifest.write_text(
        """
topology: consensus
roles:
  reviewer:
    members:
      - name: deepseek-coder
        model: zen/deepseek-v3-0324
      - name: qwen3-coder
        model: zen/qwen3-coder
      - name: mimo-vl
        model: zen/mimo-vl-7b-rl
"""
    )
    monkeypatch.setattr(roles_mod, "_TEAMS_DIR", tmp_path)
    team = roles_mod.load("t-members")
    reviewer = team.roles["reviewer"]
    assert reviewer.members is not None
    assert len(reviewer.members) == 3
    names = [m["name"] for m in reviewer.members]
    assert "deepseek-coder" in names
    assert "qwen3-coder" in names


def test_consensus_team_is_env_driven():
    """The shipped consensus team pins no models: routing comes from env."""
    team = roles_mod.load("consensus")
    assert team.roles["coder"].model == ""
    assert team.roles["reviewer"].members is None


# ---------------------------------------------------------------------------
# topology dispatch + parity test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_consensus_topology_emits_correct_event_types(monkeypatch):
    """The consensus topology must emit code, review(s), consensus, result."""
    from src import agents, topologies

    async def fake_write_code(spec, context="", provider=None, model=None, **kw):
        return {"language": "python", "code": "print(1)", "notes": "", "files": []}

    async def fake_review(member, code, **kw):
        from src.models import Review

        return Review(reviewer=member["name"], ok=True, issues=[])

    async def fake_consensus(reviews, **kw):
        from src.models import ConsensusReport

        return ConsensusReport(panel=[r.reviewer for r in reviews], summary="ok")

    async def fake_verdict(spec, code, cj, **kw):
        return {"verdict": "APPROVE", "rationale": "ok", "final_code": code, "files": []}

    monkeypatch.setattr(agents, "write_code", fake_write_code)
    monkeypatch.setattr(agents, "review_code", fake_review)
    monkeypatch.setattr(agents, "build_consensus", fake_consensus)
    monkeypatch.setattr(agents, "lead_verdict", fake_verdict)

    team = roles_mod.load("consensus")
    topo = await topologies.run(team, "test spec", run_id="test")
    events = [e async for e in topo]

    types = [e["type"] for e in events]
    assert types[0] == "code"
    # The code event carries the authoritative reviewer count for the UI.
    from src import quota

    assert events[0]["panel_size"] == len(quota.panel())
    assert "review" in types
    assert "consensus" in types
    assert types[-1] == "result"


@pytest.mark.asyncio
async def test_result_event_carries_members_and_degraded_flag(monkeypatch):
    """The result event exposes the panel members (for retry endpoints) and
    propagates the Lead's degraded flag into PipelineResult."""
    from src import agents, quota, topologies

    async def fake_write_code(spec, context="", provider=None, model=None, **kw):
        return {"language": "python", "code": "x=1", "notes": "", "files": []}

    async def fake_review(member, code, **kw):
        from src.models import Review

        return Review(reviewer=member["name"], ok=True, issues=[])

    async def fake_consensus(reviews, **kw):
        from src.models import ConsensusReport

        return ConsensusReport(panel=[r.reviewer for r in reviews], summary="ok")

    async def fake_verdict(spec, code, cj, **kw):
        return {
            "verdict": "APPROVE_WITH_CHANGES",
            "degraded": True,
            "rationale": "lead down",
            "final_code": code,
            "files": [],
        }

    monkeypatch.setattr(agents, "write_code", fake_write_code)
    monkeypatch.setattr(agents, "review_code", fake_review)
    monkeypatch.setattr(agents, "build_consensus", fake_consensus)
    monkeypatch.setattr(agents, "lead_verdict", fake_verdict)

    team = roles_mod.load("consensus")
    gen = await topologies.run(team, "spec", run_id="degraded-test")
    events = [e async for e in gen]

    result_evt = next(e for e in events if e["type"] == "result")
    assert result_evt["result"]["verdict_degraded"] is True
    assert isinstance(result_evt["members"], list)
    assert len(result_evt["members"]) == len(quota.panel())  # one per panel member
    assert {"name", "provider", "model"} <= set(result_evt["members"][0])


@pytest.mark.asyncio
async def test_pipeline_streaming_uses_team(monkeypatch):
    """pipeline.run_streaming with team_name='consensus' emits a result event."""
    from src import agents, pipeline

    async def fake_write_code(spec, context="", provider=None, model=None, **kw):
        return {"language": "python", "code": "x=1", "notes": "", "files": []}

    async def fake_review(member, code, **kw):
        from src.models import Review

        return Review(reviewer=member["name"], ok=True)

    async def fake_consensus(reviews, **kw):
        from src.models import ConsensusReport

        return ConsensusReport(panel=[r.reviewer for r in reviews], summary="ok")

    async def fake_verdict(spec, code, cj, **kw):
        return {"verdict": "APPROVE", "rationale": "ok", "final_code": code, "files": []}

    monkeypatch.setattr(agents, "write_code", fake_write_code)
    monkeypatch.setattr(agents, "review_code", fake_review)
    monkeypatch.setattr(agents, "build_consensus", fake_consensus)
    monkeypatch.setattr(agents, "lead_verdict", fake_verdict)

    events = [e async for e in pipeline.run_streaming("spec", team_name="consensus")]
    assert any(e["type"] == "result" for e in events)


@pytest.mark.asyncio
async def test_unknown_topology_raises():
    from src import topologies

    team = Team(
        name="bad", topology="nonexistent", roles={"coder": Role(name="coder", model="zen/x")}
    )
    with pytest.raises(ValueError, match="Unknown topology"):
        await topologies.run(team, "spec")


# ---------------------------------------------------------------------------
# Governor invariant: every topology LLM call is routed through governor.call
# ---------------------------------------------------------------------------


def _spy_governor(monkeypatch) -> list[str]:
    """Patch governor.call to record provider names; make_call still runs."""
    from src import governor

    seen: list[str] = []
    real_call = governor.call

    async def spy(provider_name, make_call, fallback=None, fallback_factory=None):
        seen.append(provider_name)
        return await real_call(
            provider_name, make_call, fallback=fallback, fallback_factory=fallback_factory
        )

    monkeypatch.setattr(governor, "call", spy)
    return seen


@pytest.mark.asyncio
async def test_pipeline_topology_routes_through_governor(monkeypatch):
    """run_pipeline must call the governor once per role (invariant)."""
    from src import llm, topologies

    async def fake_complete(provider, model, user, system="", max_tokens=8000):
        return "out"

    monkeypatch.setattr(llm, "complete", fake_complete)
    seen = _spy_governor(monkeypatch)

    team = roles_mod.load("sre")
    gen = await topologies.run(team, "spec", run_id="gov1")
    [e async for e in gen]

    assert len(seen) == len(team.roles)
    assert all(p for p in seen)


@pytest.mark.asyncio
async def test_loop_topology_routes_through_governor(monkeypatch):
    """run_loop must call the governor once per role per iteration (invariant)."""
    from src import llm, topologies

    async def fake_complete(provider, model, user, system="", max_tokens=8000):
        return "working"

    monkeypatch.setattr(llm, "complete", fake_complete)
    seen = _spy_governor(monkeypatch)

    team = roles_mod.load("pentest")
    [e async for e in topologies.run_loop(team, "scan", run_id="gov2", max_iterations=2)]

    assert len(seen) == len(team.roles) * 2


@pytest.mark.asyncio
async def test_consensus_topology_honours_coder_model_override(monkeypatch, tmp_path):
    """A team manifest pinning coder.model must reach write_code; an
    unpinned team leaves the choice to the env-driven quota profile."""
    from src import agents, topologies

    captured: dict = {}

    async def fake_write_code(spec, context="", provider=None, model=None, **kw):
        captured["provider"] = provider
        captured["model"] = model
        return {"language": "python", "code": "x=1", "notes": "", "files": []}

    async def fake_review(member, code, **kw):
        from src.models import Review

        return Review(reviewer=member["name"], ok=True)

    async def fake_consensus(reviews, **kw):
        from src.models import ConsensusReport

        return ConsensusReport(panel=[r.reviewer for r in reviews], summary="ok")

    async def fake_verdict(spec, code, cj, **kw):
        return {"verdict": "APPROVE", "rationale": "ok", "final_code": code, "files": []}

    monkeypatch.setattr(agents, "write_code", fake_write_code)
    monkeypatch.setattr(agents, "review_code", fake_review)
    monkeypatch.setattr(agents, "build_consensus", fake_consensus)
    monkeypatch.setattr(agents, "lead_verdict", fake_verdict)

    # Env-driven team: no override reaches write_code (quota decides).
    captured.clear()
    env_team = roles_mod.load("consensus")
    gen = await topologies.run(env_team, "spec", run_id="gov3b")
    [e async for e in gen]
    assert captured["provider"] is None
    assert captured["model"] is None

    manifest = tmp_path / "t-pinned.yaml"
    manifest.write_text(
        """
topology: consensus
roles:
  coder:
    model: zen/deepseek-v3-0324
"""
    )
    monkeypatch.setattr(roles_mod, "_TEAMS_DIR", tmp_path)
    team = roles_mod.load("t-pinned")  # pins coder: zen/deepseek-v3-0324
    gen = await topologies.run(team, "spec", run_id="gov3")
    [e async for e in gen]

    assert captured["provider"] == "zen"
    assert captured["model"] == "deepseek-v3-0324"


@pytest.mark.asyncio
async def test_consensus_topology_warns_when_sandbox_skipped(monkeypatch, caplog):
    """A requested sandbox that silently skips must surface a warning."""
    import logging

    from src import agents, topologies
    from src import sandbox as sandbox_mod
    from src.models import Artifact

    async def fake_write_code(spec, context="", provider=None, model=None, **kw):
        return {
            "language": "python",
            "code": "x=1",
            "notes": "",
            "files": [Artifact(path="main.py", language="python", content="x=1")],
        }

    async def fake_run(files, cmd, limits=None):
        return sandbox_mod.SandboxResult(skipped=True, engine="docker-missing")

    async def fake_review(member, code, **kw):
        from src.models import Review

        return Review(reviewer=member["name"], ok=True)

    async def fake_consensus(reviews, **kw):
        from src.models import ConsensusReport

        return ConsensusReport(panel=[r.reviewer for r in reviews], summary="ok")

    async def fake_verdict(spec, code, cj, **kw):
        return {"verdict": "APPROVE", "rationale": "ok", "final_code": code, "files": []}

    monkeypatch.setattr(agents, "write_code", fake_write_code)
    monkeypatch.setattr(agents, "review_code", fake_review)
    monkeypatch.setattr(agents, "build_consensus", fake_consensus)
    monkeypatch.setattr(agents, "lead_verdict", fake_verdict)
    monkeypatch.setattr(sandbox_mod, "run", fake_run)

    team = roles_mod.load("consensus-tested")  # coder.sandbox is true
    with caplog.at_level(logging.WARNING, logger="src.topologies"):
        gen = await topologies.run(team, "spec", run_id="skipwarn")
        [e async for e in gen]

    assert any("NOT executed" in rec.getMessage() for rec in caplog.records)


# ---------------------------------------------------------------------------
# Topology: pipeline  (sequential planner -> executor -> verifier)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pipeline_topology_emits_step_per_role(monkeypatch):
    """Each role must emit one 'step' event, then a final 'result'."""
    from src import llm, topologies

    async def fake_complete(provider, model, user, system="", max_tokens=8000):
        return f"output from {model}"

    monkeypatch.setattr(llm, "complete", fake_complete)

    team = roles_mod.load("sre")
    assert team.topology == "pipeline"
    topo = await topologies.run(team, "deploy redis", run_id="t1")
    events = [e async for e in topo]

    step_events = [e for e in events if e["type"] == "step"]
    role_names = [e["role"] for e in step_events]
    assert role_names == list(team.roles.keys())
    assert events[-1]["type"] == "result"


@pytest.mark.asyncio
async def test_pipeline_topology_result_contains_outputs(monkeypatch):
    from src import llm, topologies

    async def fake_complete(provider, model, user, system="", max_tokens=8000):
        return "step output"

    monkeypatch.setattr(llm, "complete", fake_complete)

    team = roles_mod.load("sre")
    topo = await topologies.run(team, "spec", run_id="t2")
    events = [e async for e in topo]
    result = events[-1]["result"]

    assert result["topology"] == "pipeline"
    assert set(result["outputs"].keys()) == set(team.roles.keys())
    for v in result["outputs"].values():
        assert v == "step output"


@pytest.mark.asyncio
async def test_pipeline_topology_accumulates_context(monkeypatch):
    """Each role should see the previous role's output in its prompt."""
    from src import llm, topologies

    received_users: list[str] = []

    async def fake_complete(provider, model, user, system="", max_tokens=8000):
        received_users.append(user)
        return f"[output of {model}]"

    monkeypatch.setattr(llm, "complete", fake_complete)

    team = roles_mod.load("sre")
    topo = await topologies.run(team, "my task", run_id="t3")
    [e async for e in topo]

    # First call has no prior context
    assert "Context so far" not in received_users[0]
    # Subsequent calls accumulate context
    for user in received_users[1:]:
        assert "Context so far" in user


# ---------------------------------------------------------------------------
# Topology: loop  (iterative recon -> exploit -> report)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_loop_topology_respects_max_iterations(monkeypatch):
    """When no role emits '[DONE]', the loop must stop at max_iterations."""
    from src import llm, topologies

    async def fake_complete(provider, model, user, system="", max_tokens=8000):
        return "still working"

    monkeypatch.setattr(llm, "complete", fake_complete)

    team = roles_mod.load("pentest")
    assert team.topology == "loop"
    max_iter = 2
    events = [
        e
        async for e in topologies.run_loop(
            team, "scan target", run_id="l1", max_iterations=max_iter
        )
    ]

    iter_events = [e for e in events if e["type"] == "iteration"]
    seen_iterations = sorted({e["i"] for e in iter_events})
    assert seen_iterations == list(range(1, max_iter + 1))
    assert events[-1]["type"] == "result"
    assert events[-1]["result"]["topology"] == "loop"


@pytest.mark.asyncio
async def test_loop_topology_stops_early_on_done(monkeypatch):
    """When a role emits '[DONE]', the loop must stop before max_iterations."""
    from src import llm, topologies

    async def fake_complete(provider, model, user, system="", max_tokens=8000):
        return "found it [DONE]"

    monkeypatch.setattr(llm, "complete", fake_complete)

    team = roles_mod.load("pentest")
    events = [e async for e in topologies.run_loop(team, "scan", run_id="l2", max_iterations=5)]

    iter_events = [e for e in events if e["type"] == "iteration"]
    # Only one step event: first role of first iteration emitted [DONE]
    assert len(iter_events) == 1
    assert iter_events[0]["i"] == 1


@pytest.mark.asyncio
async def test_loop_topology_result_contains_outputs(monkeypatch):
    from src import llm, topologies

    async def fake_complete(provider, model, user, system="", max_tokens=8000):
        return "output"

    monkeypatch.setattr(llm, "complete", fake_complete)

    team = roles_mod.load("pentest")
    events = [e async for e in topologies.run_loop(team, "spec", run_id="l3", max_iterations=1)]
    result = events[-1]["result"]

    assert result["topology"] == "loop"
    # Each role has a list of outputs (one per iteration)
    for role_name in team.roles:
        assert role_name in result["outputs"]


# ---------------------------------------------------------------------------
# Declarative layer wiring: skills / fallback / rag_ns from team manifests
# must reach the agents through the context builder (docs/teams.md promise).
# ---------------------------------------------------------------------------


def _wiring_team_manifest(tmp_path: "Path") -> None:
    manifest = tmp_path / "wiring.yaml"
    manifest.write_text(
        """
topology: consensus
sandbox: false
roles:
  coder:
    model: zen/deepseek-v3-0324
    fallback: [local]
    skills: [magicskill]
  reviewer:
    members:
      - name: rv1
        model: zen/qwen3-coder
    fallback: [local]
  consensus:
    model: zen/deepseek-v3-0324
    fallback: [local]
  lead:
    model: zen/deepseek-v3-0324
    fallback: [local]
"""
    )
    skills_dir = tmp_path / "skills"
    (skills_dir / "magicskill").mkdir(parents=True)
    (skills_dir / "magicskill" / "SKILL.md").write_text("MAGIC-SKILL-CONTENT")


@pytest.mark.asyncio
async def test_team_skills_and_fallback_reach_agents(monkeypatch, tmp_path):
    """W1+W2: role.skills -> coder prompt via context.build; role.fallback
    overrides the env-driven defaults for every consensus step."""
    from src import agents, topologies
    from src import skills as skills_mod

    _wiring_team_manifest(tmp_path)
    monkeypatch.setattr(roles_mod, "_TEAMS_DIR", tmp_path)
    monkeypatch.setattr(skills_mod, "_SKILLS_DIR", tmp_path / "skills")

    captured: dict = {}

    async def fake_write_code(
        spec, context="", provider=None, model=None, system_extra="", fallback=None, **kw
    ):
        captured["coder_system_extra"] = system_extra
        captured["coder_fallback"] = fallback
        return {"language": "python", "code": "x=1", "notes": "", "files": []}

    async def fake_review(member, code, system_extra="", fallback=None, **kw):
        captured["review_fallback"] = fallback
        from src.models import Review

        return Review(reviewer=member["name"], ok=True, issues=[])

    async def fake_consensus(reviews, fallback=None, **kw):
        captured["consensus_fallback"] = fallback
        from src.models import ConsensusReport

        return ConsensusReport(panel=[r.reviewer for r in reviews], summary="ok")

    async def fake_verdict(spec, code, cj, fallback=None, **kw):
        captured["lead_fallback"] = fallback
        return {"verdict": "APPROVE", "rationale": "ok", "final_code": code, "files": []}

    monkeypatch.setattr(agents, "write_code", fake_write_code)
    monkeypatch.setattr(agents, "review_code", fake_review)
    monkeypatch.setattr(agents, "build_consensus", fake_consensus)
    monkeypatch.setattr(agents, "lead_verdict", fake_verdict)

    team = roles_mod.load("wiring")
    topo = await topologies.run(team, "test spec", run_id="wire")
    [e async for e in topo]

    assert "MAGIC-SKILL-CONTENT" in captured["coder_system_extra"]
    assert captured["coder_fallback"] == ["local"]
    assert captured["review_fallback"] == ["local"]
    assert captured["consensus_fallback"] == ["local"]
    assert captured["lead_fallback"] == ["local"]


@pytest.mark.asyncio
async def test_empty_role_fallback_keeps_env_defaults(monkeypatch):
    """No fallback declared in the manifest -> env-driven default wins."""
    from src import agents, topologies

    captured: dict = {}

    async def fake_write_code(
        spec, context="", provider=None, model=None, system_extra="", fallback=None, **kw
    ):
        captured["coder_fallback"] = fallback
        return {"language": "python", "code": "x=1", "notes": "", "files": []}

    async def fake_review(member, code, system_extra="", fallback=None, **kw):
        from src.models import Review

        return Review(reviewer=member["name"], ok=True, issues=[])

    async def fake_consensus(reviews, fallback=None, **kw):
        from src.models import ConsensusReport

        return ConsensusReport(panel=[r.reviewer for r in reviews], summary="ok")

    async def fake_verdict(spec, code, cj, fallback=None, **kw):
        return {"verdict": "APPROVE", "rationale": "ok", "final_code": code, "files": []}

    monkeypatch.setattr(agents, "write_code", fake_write_code)
    monkeypatch.setattr(agents, "review_code", fake_review)
    monkeypatch.setattr(agents, "build_consensus", fake_consensus)
    monkeypatch.setattr(agents, "lead_verdict", fake_verdict)

    team = roles_mod.load("consensus")
    topo = await topologies.run(team, "spec", run_id="envfallback")
    [e async for e in topo]

    from src import config

    # No fallback declared -> topologies pass None and agents apply the
    # env-driven default (config.CODER_FALLBACK).
    assert captured["coder_fallback"] is None
    assert config.CODER_FALLBACK is not None or config.CODER_FALLBACK == []


@pytest.mark.asyncio
async def test_role_rag_ns_fetches_and_injects(monkeypatch, tmp_path):
    """A role declaring rag_ns must fetch RAG chunks and feed the coder
    context, and the result event must carry the sources."""
    from src import agents, topologies

    manifest = tmp_path / "ragteam.yaml"
    manifest.write_text(
        """
topology: consensus
roles:
  coder:
    model: zen/deepseek-v3-0324
    rag_ns: docs
  reviewer:
    members:
      - name: rv1
        model: zen/qwen3-coder
  consensus:
    model: zen/deepseek-v3-0324
  lead:
    model: zen/deepseek-v3-0324
"""
    )
    monkeypatch.setattr(roles_mod, "_TEAMS_DIR", tmp_path)

    hits = [{"source": "kb/api.md", "chunk_idx": 1, "content": "RAG-MAGIC-CONTENT", "score": 0.9}]

    async def fake_rag_search(query, k=5, min_score=None):
        return hits

    from src import rag as rag_mod

    monkeypatch.setattr(rag_mod, "search", fake_rag_search)

    captured: dict = {}

    async def fake_write_code(
        spec, context="", provider=None, model=None, system_extra="", fallback=None, **kw
    ):
        captured["context"] = context
        return {"language": "python", "code": "x=1", "notes": "", "files": []}

    async def fake_review(member, code, system_extra="", fallback=None, **kw):
        from src.models import Review

        return Review(reviewer=member["name"], ok=True, issues=[])

    async def fake_consensus(reviews, fallback=None, **kw):
        from src.models import ConsensusReport

        return ConsensusReport(panel=[r.reviewer for r in reviews], summary="ok")

    async def fake_verdict(spec, code, cj, fallback=None, **kw):
        return {"verdict": "APPROVE", "rationale": "ok", "final_code": code, "files": []}

    monkeypatch.setattr(agents, "write_code", fake_write_code)
    monkeypatch.setattr(agents, "review_code", fake_review)
    monkeypatch.setattr(agents, "build_consensus", fake_consensus)
    monkeypatch.setattr(agents, "lead_verdict", fake_verdict)

    team = roles_mod.load("ragteam")
    topo = await topologies.run(team, "spec", run_id="ragwire")
    events = [e async for e in topo]

    assert "RAG-MAGIC-CONTENT" in captured["context"]
    result_evt = next(e for e in events if e["type"] == "result")
    assert result_evt["result"]["rag_sources"] == hits


@pytest.mark.asyncio
async def test_pipeline_topology_injects_skills(monkeypatch):
    """W1 for the pipeline topology: role.skills reach the step system prompt."""
    from src import agents, topologies

    captured: dict = {}

    async def fake_governed_call(
        provider, model, user, system=None, max_tokens=8000, fallback=None, tools=None
    ):
        captured.setdefault("systems", []).append(system)
        captured.setdefault("fallbacks", []).append(fallback)
        return "step output"

    monkeypatch.setattr(agents, "governed_call", fake_governed_call)

    team = roles_mod.load("sre")
    events = [e async for e in topologies.run_pipeline(team, "plan infra", run_id="pw")]
    assert events[-1]["type"] == "result"

    # planner declares skills: [sre] -> system prompt carries the skill text
    assert "systems" in captured
    planner_system = captured["systems"][0]
    assert planner_system is not None and "Skill" in planner_system
    # planner_system carries the skill text; planner fallback: [] -> None
    # (agents apply the env-driven default).
    assert captured["fallbacks"][0] is None


def test_panel_members_resolve_through_providers(monkeypatch):
    """W3: member model refs go through providers.resolve_name."""
    from src import providers, topologies

    seen: list[str] = []
    real = providers.resolve_name

    def spy(ref):
        seen.append(ref)
        return real(ref)

    monkeypatch.setattr(providers, "resolve_name", spy)
    role = Role(
        name="reviewer",
        model="",
        members=[{"name": "a", "model": "zen/qwen3-coder", "max_tokens": 100}],
    )
    members = topologies._panel_members(role)
    assert seen == ["zen/qwen3-coder"]
    assert members[0]["provider"] == "zen"


# ---------------------------------------------------------------------------
# MCP tools wiring: team-level mcp_servers + role.tools (server names)
# ---------------------------------------------------------------------------


def test_mcp_servers_parsed_from_manifest(tmp_path, monkeypatch):
    """Team-level mcp_servers list is parsed into Team.mcp_servers."""
    manifest = tmp_path / "mcp-team.yaml"
    manifest.write_text(
        """
topology: consensus
mcp_servers:
  - name: serena
    transport: stdio
    command: ["uvx", "serena"]
  - name: remote
    transport: http
    url: https://tools.example.com/mcp
roles:
  coder:
    model: zen/deepseek-v3-0324
  reviewer:
    members:
      - name: rv1
        model: zen/qwen3-coder
  consensus:
    model: zen/deepseek-v3-0324
  lead:
    model: zen/deepseek-v3-0324
"""
    )
    monkeypatch.setattr(roles_mod, "_TEAMS_DIR", tmp_path)
    team = roles_mod.load("mcp-team")
    assert len(team.mcp_servers) == 2
    assert team.mcp_servers[0]["name"] == "serena"
    assert team.mcp_servers[1]["transport"] == "http"


def test_mcp_servers_default_empty(tmp_path, monkeypatch):
    manifest = tmp_path / "plain.yaml"
    manifest.write_text(
        """
topology: consensus
roles:
  coder:
    model: zen/deepseek-v3-0324
"""
    )
    monkeypatch.setattr(roles_mod, "_TEAMS_DIR", tmp_path)
    team = roles_mod.load("plain")
    assert team.mcp_servers == []


def test_role_servers_resolved_from_names(tmp_path, monkeypatch):
    """role.tools holds server NAMES; a role helper resolves them to configs."""
    from src.topologies import role_servers

    manifest = tmp_path / "mcp-role.yaml"
    manifest.write_text(
        """
topology: consensus
mcp_servers:
  - name: serena
    transport: stdio
    command: ["uvx", "serena"]
  - name: other
    transport: stdio
    command: ["echo"]
roles:
  coder:
    model: zen/deepseek-v3-0324
    tools: [serena]
"""
    )
    monkeypatch.setattr(roles_mod, "_TEAMS_DIR", tmp_path)
    team = roles_mod.load("mcp-role")
    role = team.roles["coder"]

    servers = role_servers(team, role)
    assert [s["name"] for s in servers] == ["serena"]


def test_role_servers_empty_when_no_tools(tmp_path, monkeypatch):
    from src.topologies import role_servers

    team = roles_mod.load("consensus")
    assert role_servers(team, team.roles["coder"]) == []


def test_role_tools_without_servers_get_no_runtime(tmp_path, monkeypatch):
    """role.tools referencing servers absent from mcp_servers -> no runtime."""
    from src.topologies import _runtime_for_role

    manifest = tmp_path / "orphan.yaml"
    manifest.write_text(
        """
topology: consensus
roles:
  coder:
    model: zen/deepseek-v3-0324
    tools: [ghost]
"""
    )
    monkeypatch.setattr(roles_mod, "_TEAMS_DIR", tmp_path)
    team = roles_mod.load("orphan")
    assert _runtime_for_role(team, team.roles["coder"]) is None


@pytest.mark.asyncio
async def test_tools_reach_coder_through_mcp_manager(monkeypatch, tmp_path):
    """End-to-end: role.tools + team.mcp_servers -> MCPClientManager started,
    definitions in the coder system prompt, tool callable in write_code."""
    from src import agents, mcp_client, topologies

    manifest = tmp_path / "mcp-e2e.yaml"
    manifest.write_text(
        """
topology: consensus
mcp_servers:
  - name: fake
    transport: stdio
    command: ["true"]
roles:
  coder:
    model: zen/deepseek-v3-0324
    tools: [fake]
  reviewer:
    members:
      - name: rv1
        model: zen/qwen3-coder
  consensus:
    model: zen/deepseek-v3-0324
  lead:
    model: zen/deepseek-v3-0324
"""
    )
    monkeypatch.setattr(roles_mod, "_TEAMS_DIR", tmp_path)

    started: list[list[dict]] = []

    class FakeManager:
        def __init__(self, servers):
            started.append(servers)
            self.servers = servers

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def list_tools(self):
            return [{"name": "fake_tool", "description": "does fake things", "input_schema": {}}]

        async def call_tool(self, name, arguments):
            return f"ran {name}"

    monkeypatch.setattr(mcp_client, "MCPClientManager", FakeManager)
    monkeypatch.setattr(topologies, "MCPClientManager", FakeManager)

    captured: dict = {}

    async def fake_write_code(
        spec,
        context="",
        provider=None,
        model=None,
        system_extra="",
        fallback=None,
        tools=None,
        **kw,
    ):
        captured["tools"] = tools
        return {"language": "python", "code": "x=1", "notes": "", "files": []}

    async def fake_review(member, code, **kw):
        from src.models import Review

        return Review(reviewer=member["name"], ok=True, issues=[])

    async def fake_consensus(reviews, **kw):
        from src.models import ConsensusReport

        return ConsensusReport(panel=[r.reviewer for r in reviews], summary="ok")

    async def fake_verdict(spec, code, cj, **kw):
        return {"verdict": "APPROVE", "rationale": "ok", "final_code": code, "files": []}

    monkeypatch.setattr(agents, "write_code", fake_write_code)
    monkeypatch.setattr(agents, "review_code", fake_review)
    monkeypatch.setattr(agents, "build_consensus", fake_consensus)
    monkeypatch.setattr(agents, "lead_verdict", fake_verdict)

    team = roles_mod.load("mcp-e2e")
    gen = await topologies.run(team, "spec", run_id="mcp-e2e")
    [e async for e in gen]

    tools = captured["tools"]
    assert [t["name"] for t in tools.definitions] == ["fake_tool"]
    assert await tools.call("fake_tool", {}) == "ran fake_tool"
    assert started and started[0][0]["name"] == "fake"


@pytest.mark.asyncio
async def test_no_tools_no_manager(monkeypatch):
    """No role declares tools -> MCPClientManager never instantiated."""
    from src import agents, mcp_client, topologies

    def boom(*a, **kw):
        raise AssertionError("MCPClientManager must not be instantiated")

    monkeypatch.setattr(mcp_client, "MCPClientManager", boom)
    monkeypatch.setattr(topologies, "MCPClientManager", boom)

    async def fake_write_code(spec, context="", provider=None, model=None, **kw):
        return {"language": "python", "code": "x=1", "notes": "", "files": []}

    async def fake_review(member, code, **kw):
        from src.models import Review

        return Review(reviewer=member["name"], ok=True, issues=[])

    async def fake_consensus(reviews, **kw):
        from src.models import ConsensusReport

        return ConsensusReport(panel=[r.reviewer for r in reviews], summary="ok")

    async def fake_verdict(spec, code, cj, **kw):
        return {"verdict": "APPROVE", "rationale": "ok", "final_code": code, "files": []}

    monkeypatch.setattr(agents, "write_code", fake_write_code)
    monkeypatch.setattr(agents, "review_code", fake_review)
    monkeypatch.setattr(agents, "build_consensus", fake_consensus)
    monkeypatch.setattr(agents, "lead_verdict", fake_verdict)

    team = roles_mod.load("consensus")
    topo = await topologies.run(team, "spec", run_id="nomanager")
    [e async for e in topo]  # must not raise
