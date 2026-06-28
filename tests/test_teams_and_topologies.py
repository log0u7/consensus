"""Tests for roles/teams YAML loading and topology dispatch.

Parity test: the 'consensus' team must emit exactly the same event types
as the original hardcoded pipeline (code, review*, consensus, result).
"""

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


def test_reviewer_members_parsed():
    team = roles_mod.load("consensus")
    reviewer = team.roles["reviewer"]
    assert reviewer.members is not None
    assert len(reviewer.members) == 3
    names = [m["name"] for m in reviewer.members]
    assert "deepseek-coder" in names
    assert "qwen3-coder" in names


# ---------------------------------------------------------------------------
# topology dispatch + parity test
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_consensus_topology_emits_correct_event_types(monkeypatch):
    """The consensus topology must emit code, review(s), consensus, result."""
    from src import agents, topologies

    async def fake_write_code(spec, context=""):
        return {"language": "python", "code": "print(1)", "notes": "", "files": []}

    async def fake_review(member, code):
        from src.models import Review
        return Review(reviewer=member["name"], ok=True, issues=[])

    async def fake_consensus(reviews):
        from src.models import ConsensusReport
        return ConsensusReport(panel=[r.reviewer for r in reviews], summary="ok")

    async def fake_verdict(spec, code, cj):
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
    assert "review" in types
    assert "consensus" in types
    assert types[-1] == "result"


@pytest.mark.asyncio
async def test_pipeline_streaming_uses_team(monkeypatch):
    """pipeline.run_streaming with team_name='consensus' emits a result event."""
    from src import agents, pipeline

    async def fake_write_code(spec, context=""):
        return {"language": "python", "code": "x=1", "notes": "", "files": []}

    async def fake_review(member, code):
        from src.models import Review
        return Review(reviewer=member["name"], ok=True)

    async def fake_consensus(reviews):
        from src.models import ConsensusReport
        return ConsensusReport(panel=[r.reviewer for r in reviews], summary="ok")

    async def fake_verdict(spec, code, cj):
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
        name="bad", topology="nonexistent",
        roles={"coder": Role(name="coder", model="zen/x")}
    )
    with pytest.raises(ValueError, match="Unknown topology"):
        await topologies.run(team, "spec")
