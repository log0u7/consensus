"""Tests for the retry endpoints (/api/run/retry/reviewer and /lead).

Uses httpx.ASGITransport against the FastAPI app with monkeypatched agents,
so no LLM/provider is touched.
"""

import json

import httpx
import pytest
from src import api
from src.models import ConsensusReport, PipelineResult, Review
from src.sessions import store


def _seed(reviews: list[Review]) -> str:
    result = PipelineResult(
        spec="the-spec",
        code="x=1",
        language="python",
        reviews=reviews,
        consensus=ConsensusReport(
            panel=[r.reviewer for r in reviews if r.ok], issues=[], summary="old"
        ),
    )
    members = [
        {"name": "r1", "provider": "zen", "model": "m1"},
        {"name": "r2", "provider": "zen", "model": "m2"},
    ]
    return store.create({"result": result, "system": "", "history": [], "members": members})


def _sse_events(text: str) -> list[dict]:
    out = []
    for line in text.splitlines():
        if line.startswith("data:"):
            out.append(json.loads(line[5:].strip()))
    return out


@pytest.mark.asyncio
async def test_retry_reviewer_success_replays_consensus_and_lead(monkeypatch):
    from src import agents

    reviews = [
        Review(reviewer="r1", ok=True, issues=[]),
        Review(reviewer="r2", ok=False, error="429"),
    ]
    sid = _seed(reviews)
    seen = {}

    async def fake_review(member, code):
        seen["member"] = member
        seen["code"] = code
        return Review(reviewer="r2", ok=True, issues=[])

    async def fake_consensus(revs):
        seen["panel"] = [r.reviewer for r in revs if r.ok]
        return ConsensusReport(panel=seen["panel"], issues=[], summary="new")

    async def fake_lead(spec, code, cj):
        seen["spec"], seen["lead_code"] = spec, code
        return {
            "verdict": "APPROVE",
            "degraded": False,
            "rationale": "ok",
            "final_code": "x=2",
            "files": [],
        }

    monkeypatch.setattr(agents, "review_code", fake_review)
    monkeypatch.setattr(agents, "build_consensus", fake_consensus)
    monkeypatch.setattr(agents, "lead_verdict", fake_lead)

    transport = httpx.ASGITransport(app=api.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.post("/api/run/retry/reviewer", json={"session_id": sid, "reviewer": "r2"})
    assert r.status_code == 200
    evts = _sse_events(r.text)
    types = [e["type"] for e in evts]
    assert types == ["review", "consensus", "result"]

    # consensus replayed with the UPDATED panel (both reviewers ok)
    assert seen["panel"] == ["r1", "r2"]
    assert seen["member"]["name"] == "r2" and seen["code"] == "x=1"
    assert seen["spec"] == "the-spec" and seen["lead_code"] == "x=1"

    # session updated in place
    sess = store.get(sid)
    res = sess["result"]
    assert res.reviews[1].ok is True
    assert res.verdict == "APPROVE" and res.verdict_degraded is False
    assert res.final_code == "x=2"
    assert res.consensus.summary == "new"


@pytest.mark.asyncio
async def test_retry_reviewer_failure_keeps_state(monkeypatch):
    from src import agents

    reviews = [Review(reviewer="r2", ok=False, error="429")]
    sid = _seed(reviews)

    async def fake_review(member, code):
        return Review(reviewer="r2", ok=False, error="429 again")

    monkeypatch.setattr(agents, "review_code", fake_review)

    transport = httpx.ASGITransport(app=api.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.post("/api/run/retry/reviewer", json={"session_id": sid, "reviewer": "r2"})
    evts = _sse_events(r.text)
    assert evts[-1]["type"] == "error"
    # old state untouched
    res = store.get(sid)["result"]
    assert res.reviews[0].error == "429"


@pytest.mark.asyncio
async def test_retry_lead_updates_verdict(monkeypatch):
    from src import agents

    reviews = [Review(reviewer="r1", ok=True, issues=[])]
    sid = _seed(reviews)

    async def fake_lead(spec, code, cj):
        assert spec == "the-spec" and code == "x=1"
        assert "old" in cj  # consensus json from the session
        return {
            "verdict": "REJECT",
            "degraded": False,
            "rationale": "worse",
            "final_code": "x=3",
            "files": [],
        }

    monkeypatch.setattr(agents, "lead_verdict", fake_lead)

    transport = httpx.ASGITransport(app=api.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.post("/api/run/retry/lead", json={"session_id": sid})
    evts = _sse_events(r.text)
    assert [e["type"] for e in evts] == ["result"]
    res = store.get(sid)["result"]
    assert res.verdict == "REJECT" and res.final_code == "x=3"


@pytest.mark.asyncio
async def test_retry_unknown_session_and_reviewer():
    transport = httpx.ASGITransport(app=api.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r1 = await c.post("/api/run/retry/reviewer", json={"session_id": "nope", "reviewer": "r1"})
        assert r1.status_code == 404

        sid = _seed([Review(reviewer="r1", ok=True, issues=[])])
        r2 = await c.post("/api/run/retry/reviewer", json={"session_id": sid, "reviewer": "ghost"})
        assert r2.status_code == 404
        r3 = await c.post("/api/run/retry/lead", json={"session_id": "nope"})
        assert r3.status_code == 404
