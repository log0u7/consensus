"""Unit tests for src/pipeline.py.

All agent calls are replaced by fakes so no network is needed.
Tests cover:
  - run_streaming: event sequence and types
  - run (non-streaming wrapper): returns a PipelineResult with a CostSummary
  - CostSummary aggregation helper
  - RAG path is bypassed gracefully when use_rag=False (default)
  - lead_system_for: builds the Lead system prompt from a PipelineResult
"""

import pytest
from src import agents, pipeline
from src.models import ConsensusReport, CostSummary, PipelineResult, Usage  # noqa: F401 (all used)

# ---------------------------------------------------------------------------
# Fixtures / shared fakes
# ---------------------------------------------------------------------------

def _patch_agents(monkeypatch):
    """Monkeypatch all four agent functions with fast fakes."""

    async def fake_write_code(spec, context=""):
        return {"language": "python", "code": "x = 1", "notes": "", "files": []}

    async def fake_review(member, code):
        from src.models import Review
        return Review(reviewer=member["name"], ok=True, issues=[])

    async def fake_consensus(reviews):
        return ConsensusReport(panel=[r.reviewer for r in reviews], summary="ok")

    async def fake_verdict(spec, code, cj):
        return {"verdict": "APPROVE", "rationale": "fine", "final_code": code, "files": []}

    monkeypatch.setattr(agents, "write_code",      fake_write_code)
    monkeypatch.setattr(agents, "review_code",     fake_review)
    monkeypatch.setattr(agents, "build_consensus", fake_consensus)
    monkeypatch.setattr(agents, "lead_verdict",    fake_verdict)


# ---------------------------------------------------------------------------
# run_streaming
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_streaming_emits_code_event(monkeypatch):
    _patch_agents(monkeypatch)
    events = [e async for e in pipeline.run_streaming("write hello world")]
    code_events = [e for e in events if e["type"] == "code"]
    assert len(code_events) == 1
    assert code_events[0]["code"] == "x = 1"


@pytest.mark.asyncio
async def test_run_streaming_emits_review_events(monkeypatch):
    _patch_agents(monkeypatch)
    events = [e async for e in pipeline.run_streaming("spec")]
    review_events = [e for e in events if e["type"] == "review"]
    # Default team has 3 reviewers
    assert len(review_events) >= 1


@pytest.mark.asyncio
async def test_run_streaming_emits_consensus_event(monkeypatch):
    _patch_agents(monkeypatch)
    events = [e async for e in pipeline.run_streaming("spec")]
    assert any(e["type"] == "consensus" for e in events)


@pytest.mark.asyncio
async def test_run_streaming_emits_result_last(monkeypatch):
    _patch_agents(monkeypatch)
    events = [e async for e in pipeline.run_streaming("spec")]
    assert events[-1]["type"] == "result"


@pytest.mark.asyncio
async def test_run_streaming_event_order(monkeypatch):
    _patch_agents(monkeypatch)
    events = [e async for e in pipeline.run_streaming("spec")]
    types = [e["type"] for e in events]
    # code must come before review, review before consensus, consensus before result
    assert types.index("code") < types.index("review")
    assert types.index("review") < types.index("consensus")
    assert types.index("consensus") < types.index("result")


@pytest.mark.asyncio
async def test_run_streaming_result_has_verdict(monkeypatch):
    _patch_agents(monkeypatch)
    events = [e async for e in pipeline.run_streaming("spec")]
    result_event = next(e for e in events if e["type"] == "result")
    assert result_event["result"]["verdict"] == "APPROVE"


@pytest.mark.asyncio
async def test_run_streaming_no_rag_by_default(monkeypatch):
    """use_rag defaults to False: RAG module must never be imported/called."""
    _patch_agents(monkeypatch)
    rag_called = {"v": False}

    async def fake_rag_search(*a, **kw):
        rag_called["v"] = True
        return []

    # Patch the rag module that pipeline imports lazily
    import src.rag as rag_mod
    monkeypatch.setattr(rag_mod, "search", fake_rag_search)

    [e async for e in pipeline.run_streaming("spec", use_rag=False)]
    assert not rag_called["v"]


# ---------------------------------------------------------------------------
# run (non-streaming wrapper)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_returns_pipeline_result(monkeypatch):
    _patch_agents(monkeypatch)
    result = await pipeline.run("write a function")
    assert isinstance(result, PipelineResult)
    assert result.verdict == "APPROVE"
    assert result.spec == "write a function"


@pytest.mark.asyncio
async def test_run_result_has_cost_summary(monkeypatch):
    _patch_agents(monkeypatch)
    result = await pipeline.run("spec")
    assert isinstance(result.cost_summary, CostSummary)
    assert result.cost_summary.calls >= 0


@pytest.mark.asyncio
async def test_run_result_has_consensus(monkeypatch):
    _patch_agents(monkeypatch)
    result = await pipeline.run("spec")
    assert isinstance(result.consensus, ConsensusReport)
    assert result.consensus.summary == "ok"


# ---------------------------------------------------------------------------
# summarize_usage helper
# ---------------------------------------------------------------------------

def test_summarize_usage_empty():
    cs = pipeline.summarize_usage([])
    assert cs.calls == 0
    assert cs.input_tokens == 0
    assert cs.output_tokens == 0
    assert cs.cost == 0.0
    assert cs.cost_known is False


def test_summarize_usage_aggregates_tokens():
    usages = [
        Usage(input_tokens=100, output_tokens=50, cost=0.001),
        Usage(input_tokens=200, output_tokens=80, cost=0.002),
        Usage(input_tokens=50,  output_tokens=20),  # no cost
    ]
    cs = pipeline.summarize_usage(usages)
    assert cs.calls == 3
    assert cs.input_tokens == 350
    assert cs.output_tokens == 150
    assert cs.cost == pytest.approx(0.003, abs=1e-6)
    assert cs.cost_known is True  # at least one has cost


def test_summarize_usage_no_cost_known():
    usages = [
        Usage(input_tokens=100, output_tokens=50),
        Usage(input_tokens=200, output_tokens=80),
    ]
    cs = pipeline.summarize_usage(usages)
    assert cs.cost_known is False
    assert cs.cost == 0.0


# ---------------------------------------------------------------------------
# lead_system_for
# ---------------------------------------------------------------------------

def test_lead_system_for_includes_spec_and_code():
    result = PipelineResult(
        spec="build an API",
        code="def main(): pass",
        consensus=ConsensusReport(panel=["r1"], summary="clean"),
        verdict="APPROVE",
        rationale="ok",
    )
    system = pipeline.lead_system_for(result)
    assert "build an API" in system
    assert "def main()" in system


def test_lead_system_for_uses_final_code_when_set():
    result = PipelineResult(
        spec="spec",
        code="original code",
        final_code="improved code",
        consensus=ConsensusReport(),
    )
    system = pipeline.lead_system_for(result)
    assert "improved code" in system
    assert "original code" not in system
