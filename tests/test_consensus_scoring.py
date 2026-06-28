import pytest
from src import agents
from src.models import Review


@pytest.mark.asyncio
async def test_consensus_validates_flagged_by_and_clamps(monkeypatch):
    reviews = [
        Review(reviewer="a", ok=True),
        Review(reviewer="b", ok=True),
        Review(reviewer="c", ok=True),
    ]

    # The consensus model hallucinates an unknown reviewer and a duplicate.
    async def fake_complete_json(make_call, retries=2):
        return {
            "issues": [
                {
                    "title": "SQL injection",
                    "severity": "critical",
                    "category": "security",
                    "description": "x",
                    "flagged_by": ["a", "a", "b", "ghost"],  # dup + unknown
                }
            ],
            "summary": "ok",
        }

    monkeypatch.setattr(agents.llm, "complete_json", fake_complete_json)
    report = await agents.build_consensus(reviews)
    assert len(report.issues) == 1
    issue = report.issues[0]
    # 'ghost' dropped, 'a' deduped -> flagged_by = {a, b}; score = 2/3.
    assert set(issue.flagged_by) == {"a", "b"}
    assert issue.consensus_score == pytest.approx(2 / 3, abs=1e-3)
    assert 0.0 <= issue.consensus_score <= 1.0


@pytest.mark.asyncio
async def test_consensus_no_reviewers():
    report = await agents.build_consensus([Review(reviewer="a", ok=False, error="boom")])
    assert report.issues == []
    assert "No reviewer" in report.summary
