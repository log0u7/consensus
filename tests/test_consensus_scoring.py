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


@pytest.mark.asyncio
async def test_consensus_single_reviewer_score_is_one(monkeypatch):
    """A single reviewer flagging an issue gives score = 1/1 = 1.0."""
    reviews = [Review(reviewer="solo", ok=True)]

    async def fake_complete_json(make_call, retries=2):
        return {
            "issues": [{
                "title": "bug", "severity": "high", "category": "correctness",
                "description": "x", "flagged_by": ["solo"],
            }],
            "summary": "one reviewer",
        }

    monkeypatch.setattr(agents.llm, "complete_json", fake_complete_json)
    report = await agents.build_consensus(reviews)
    assert len(report.issues) == 1
    assert report.issues[0].consensus_score == pytest.approx(1.0)
    assert report.issues[0].flagged_by == ["solo"]


@pytest.mark.asyncio
async def test_consensus_equal_score_sorted_by_severity(monkeypatch):
    """Issues with the same score must be sorted critical > high > medium > low."""
    reviews = [Review(reviewer="a", ok=True), Review(reviewer="b", ok=True)]

    async def fake_complete_json(make_call, retries=2):
        return {
            "issues": [
                {"title": "low-issue",    "severity": "low",      "category": "style",
                 "description": "d", "flagged_by": ["a", "b"]},
                {"title": "critical-issue","severity": "critical", "category": "security",
                 "description": "d", "flagged_by": ["a", "b"]},
                {"title": "medium-issue", "severity": "medium",   "category": "correctness",
                 "description": "d", "flagged_by": ["a", "b"]},
            ],
            "summary": "ok",
        }

    monkeypatch.setattr(agents.llm, "complete_json", fake_complete_json)
    report = await agents.build_consensus(reviews)
    titles = [i.title for i in report.issues]
    assert titles == ["critical-issue", "medium-issue", "low-issue"]


@pytest.mark.asyncio
async def test_consensus_panel_names_reflect_ok_reviewers(monkeypatch):
    """report.panel must contain only the reviewers that answered (ok=True)."""
    reviews = [
        Review(reviewer="ok1",  ok=True),
        Review(reviewer="bad1", ok=False, error="timeout"),
        Review(reviewer="ok2",  ok=True),
    ]

    async def fake_complete_json(make_call, retries=2):
        return {"issues": [], "summary": "clean"}

    monkeypatch.setattr(agents.llm, "complete_json", fake_complete_json)
    report = await agents.build_consensus(reviews)
    assert set(report.panel) == {"ok1", "ok2"}
    assert "bad1" not in report.panel
