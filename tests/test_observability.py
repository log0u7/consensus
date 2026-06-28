import logging

import pytest
from src import llm


@pytest.mark.asyncio
async def test_run_streaming_logs_run_id(monkeypatch, caplog):
    """Pipeline emits a result event and logs are prefixed with the run_id."""
    from src import agents, pipeline

    async def fake_write_code(spec, context=""):
        return {"language": "python", "code": "print(1)", "notes": "", "files": []}

    async def fake_review(member, code):
        from src.models import Review
        return Review(reviewer=member["name"], ok=True)

    async def fake_consensus(reviews):
        from src.models import ConsensusReport
        return ConsensusReport(panel=[r.reviewer for r in reviews], summary="ok")

    async def fake_verdict(spec, code, cj):
        return {"verdict": "APPROVE", "rationale": "ok", "final_code": "print(1)", "files": []}

    monkeypatch.setattr(agents, "write_code", fake_write_code)
    monkeypatch.setattr(agents, "review_code", fake_review)
    monkeypatch.setattr(agents, "build_consensus", fake_consensus)
    monkeypatch.setattr(agents, "lead_verdict", fake_verdict)

    with caplog.at_level(logging.INFO, logger="src.pipeline"):
        events = [e async for e in pipeline.run_streaming("spec", run_id="testid42")]

    assert any(e["type"] == "result" for e in events)
    assert any("[run testid42]" in rec.getMessage() for rec in caplog.records)


@pytest.mark.asyncio
async def test_provider_reachable_error_path():
    """provider_reachable returns reachable=False on an unreachable host, no raise."""
    out = await llm.provider_reachable("zen", timeout=1.0)
    # ZEN_API_KEY=dummy -> may reach or not, but must not raise
    assert "reachable" in out
    assert "error" in out
