"""Unit tests for src/agents.py.

All LLM calls are replaced by synchronous fakes via monkeypatch so no network
is needed.  Tests cover:
  - write_code: single-file and multi-file responses
  - review_code: resilience when a reviewer fails
  - build_consensus: flagged_by validation, dedup, score clamping
  - lead_verdict: degraded verdict when JSON cannot be parsed
"""

import pytest
from src import agents
from src.models import Review

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_fake_json_obj(return_value: dict):
    """Return a coroutine that ignores its make_call argument and yields the dict."""

    async def _fake(make_call, retries=2):
        return return_value

    return _fake


def _make_invoking_fake_json_obj(return_value: dict):
    """Like _make_fake_json_obj but consumes make_call once, so wrapped layers
    (governor.call) are actually exercised."""

    async def _fake(make_call, retries=2):
        await make_call(0)
        return return_value

    return _fake


# ---------------------------------------------------------------------------
# write_code
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_write_code_single_file(monkeypatch):
    monkeypatch.setattr(
        agents.llm,
        "complete_json_obj",
        _make_fake_json_obj({"language": "python", "code": "print(1)", "notes": "ok"}),
    )
    result = await agents.write_code("print 1")
    assert result["language"] == "python"
    assert result["code"] == "print(1)"
    assert result["files"] == []


@pytest.mark.asyncio
async def test_write_code_multi_file(monkeypatch):
    files_payload = [
        {"path": "main.py", "language": "python", "content": "import mod"},
        {"path": "mod.py", "language": "python", "content": "x = 1"},
    ]
    monkeypatch.setattr(
        agents.llm,
        "complete_json_obj",
        _make_fake_json_obj({"language": "python", "files": files_payload, "notes": "split"}),
    )
    result = await agents.write_code("multi-file app")
    assert len(result["files"]) == 2
    assert result["files"][0].path == "main.py"
    # code blob must be a concatenation of the files
    assert "main.py" in result["code"]
    assert "mod.py" in result["code"]


@pytest.mark.asyncio
async def test_write_code_strips_invalid_file(monkeypatch):
    """An artifact with a path containing '..' must be sanitized (dropped or cleaned)."""
    files_payload = [
        {"path": "../evil.py", "language": "python", "content": "x=1"},
        {"path": "safe.py", "language": "python", "content": "x=2"},
    ]
    monkeypatch.setattr(
        agents.llm,
        "complete_json_obj",
        _make_fake_json_obj({"language": "python", "files": files_payload}),
    )
    result = await agents.write_code("exploit")
    # safe.py must be present; the dotdot path must be sanitized
    paths = [f.path for f in result["files"]]
    assert "safe.py" in paths
    for p in paths:
        assert ".." not in p


@pytest.mark.asyncio
async def test_write_code_with_context(monkeypatch):
    captured = {}

    async def _fake(make_call, retries=2):
        # The governor wraps the factory; call it at attempt 0 to capture the
        # prompt that would be sent. We intercept complete_json_obj before the
        # governor so we just need the return value.
        captured["called"] = True
        return {"language": "python", "code": "x=1", "notes": ""}

    monkeypatch.setattr(agents.llm, "complete_json_obj", _fake)
    await agents.write_code("do x", context="some rag context")
    assert captured.get("called")


# ---------------------------------------------------------------------------
# review_code
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_review_code_ok(monkeypatch):
    monkeypatch.setattr(
        agents.llm,
        "complete_json_obj",
        _make_fake_json_obj(
            {
                "issues": [
                    {
                        "title": "SQL injection",
                        "severity": "critical",
                        "category": "security",
                        "location": "line 5",
                        "description": "unescaped input",
                    },
                ],
                "overall": "looks risky",
            }
        ),
    )
    member = {"name": "r1", "provider": "zen", "model": "deepseek-v3-0324"}
    review = await agents.review_code(member, "code here")
    assert review.ok is True
    assert review.reviewer == "r1"
    assert len(review.issues) == 1
    assert review.issues[0].severity == "critical"


@pytest.mark.asyncio
async def test_review_code_failure_returns_ok_false(monkeypatch):
    """A reviewer that raises must return Review(ok=False) without propagating."""

    async def _boom(make_call, retries=2):
        raise RuntimeError("network error")

    monkeypatch.setattr(agents.llm, "complete_json_obj", _boom)
    member = {"name": "r_bad", "provider": "zen", "model": "deepseek-v3-0324"}
    review = await agents.review_code(member, "some code")
    assert review.ok is False
    assert review.reviewer == "r_bad"
    assert "RuntimeError" in (review.error or "")


@pytest.mark.asyncio
async def test_review_code_normalizes_severity(monkeypatch):
    """Unknown severity values are normalised to the default 'medium'."""
    monkeypatch.setattr(
        agents.llm,
        "complete_json_obj",
        _make_fake_json_obj(
            {
                "issues": [
                    {
                        "title": "ok issue",
                        "severity": "CRITICAL",  # uppercase -> normalised
                        "category": "security",
                        "location": "line 1",
                        "description": "d",
                    },
                    {
                        "title": "unknown sev",
                        "severity": "bogus",  # unknown -> default
                        "category": "style",
                        "location": "line 2",
                        "description": "d",
                    },
                ],
                "overall": "mixed",
            }
        ),
    )
    member = {"name": "r2", "provider": "zen", "model": "x"}
    review = await agents.review_code(member, "code")
    assert review.ok is True
    assert len(review.issues) == 2
    assert review.issues[0].severity == "critical"  # normalised from CRITICAL
    assert review.issues[1].severity == "medium"  # default for unknown


# ---------------------------------------------------------------------------
# build_consensus
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_consensus_all_failed_reviewers():
    """All reviewers failed -> empty report with a message."""
    bad_reviews = [Review(reviewer="r1", ok=False, error="timeout")]
    report = await agents.build_consensus(bad_reviews)
    assert report.issues == []
    assert "No reviewer" in report.summary


@pytest.mark.asyncio
async def test_consensus_score_clamped_to_one(monkeypatch):
    """Score must be clamped to 1.0 even if the model inflates flagged_by."""
    reviews = [Review(reviewer="a", ok=True), Review(reviewer="b", ok=True)]

    monkeypatch.setattr(
        agents.llm,
        "complete_json_obj",
        _make_fake_json_obj(
            {
                "issues": [
                    {
                        "title": "overflow",
                        "severity": "high",
                        "category": "correctness",
                        "description": "x",
                        # 4 names for a 2-person panel (after dedup/filter -> a, b -> 2/2 = 1.0)
                        "flagged_by": ["a", "b", "a", "b"],
                    }
                ],
                "summary": "ok",
            }
        ),
    )
    report = await agents.build_consensus(reviews)
    assert len(report.issues) == 1
    assert report.issues[0].consensus_score <= 1.0
    assert report.issues[0].consensus_score == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_consensus_unknown_reviewer_dropped(monkeypatch):
    reviews = [Review(reviewer="alice", ok=True), Review(reviewer="bob", ok=True)]

    monkeypatch.setattr(
        agents.llm,
        "complete_json_obj",
        _make_fake_json_obj(
            {
                "issues": [
                    {
                        "title": "issue",
                        "severity": "medium",
                        "category": "correctness",
                        "description": "d",
                        "flagged_by": ["alice", "ghost"],  # ghost not in panel
                    }
                ],
                "summary": "ok",
            }
        ),
    )
    report = await agents.build_consensus(reviews)
    assert len(report.issues) == 1
    assert "ghost" not in report.issues[0].flagged_by
    assert "alice" in report.issues[0].flagged_by
    # score = 1/2
    assert report.issues[0].consensus_score == pytest.approx(0.5, abs=1e-3)


@pytest.mark.asyncio
async def test_consensus_sorted_by_score_then_severity(monkeypatch):
    reviews = [
        Review(reviewer="a", ok=True),
        Review(reviewer="b", ok=True),
        Review(reviewer="c", ok=True),
    ]

    monkeypatch.setattr(
        agents.llm,
        "complete_json_obj",
        _make_fake_json_obj(
            {
                "issues": [
                    {
                        "title": "low-conf-critical",
                        "severity": "critical",
                        "category": "security",
                        "description": "d",
                        "flagged_by": ["a"],
                    },  # score 1/3
                    {
                        "title": "high-conf-medium",
                        "severity": "medium",
                        "category": "correctness",
                        "description": "d",
                        "flagged_by": ["a", "b", "c"],
                    },  # score 3/3 = 1.0
                    {
                        "title": "mid-conf-high",
                        "severity": "high",
                        "category": "security",
                        "description": "d",
                        "flagged_by": ["a", "b"],
                    },  # score 2/3
                ],
                "summary": "ok",
            }
        ),
    )
    report = await agents.build_consensus(reviews)
    titles = [i.title for i in report.issues]
    assert titles[0] == "high-conf-medium"  # highest score first
    assert titles[1] == "mid-conf-high"
    assert titles[2] == "low-conf-critical"


# ---------------------------------------------------------------------------
# lead_verdict
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lead_verdict_ok(monkeypatch):
    monkeypatch.setattr(
        agents.llm,
        "complete_json_obj",
        _make_fake_json_obj(
            {
                "verdict": "APPROVE",
                "rationale": "LGTM",
                "final_code": "print(1)",
                "files": [],
            }
        ),
    )
    result = await agents.lead_verdict("spec", "code", "{}")
    assert result["verdict"] == "APPROVE"
    assert result["rationale"] == "LGTM"
    assert result["final_code"] == "print(1)"


@pytest.mark.asyncio
async def test_lead_verdict_degraded_on_parse_failure(monkeypatch):
    """When complete_json_obj raises ValueError, lead_verdict returns a degraded
    verdict instead of propagating the exception."""

    async def _fail(make_call, retries=2):
        raise ValueError("truncated JSON")

    monkeypatch.setattr(agents.llm, "complete_json_obj", _fail)
    result = await agents.lead_verdict("spec", "code", "{}")
    assert result["verdict"] == "APPROVE_WITH_CHANGES"
    assert "parsed" in result["rationale"].lower() or "lead" in result["rationale"].lower()
    assert result["final_code"] == ""
    assert result["files"] == []


@pytest.mark.asyncio
async def test_lead_verdict_multi_file(monkeypatch):
    monkeypatch.setattr(
        agents.llm,
        "complete_json_obj",
        _make_fake_json_obj(
            {
                "verdict": "APPROVE",
                "rationale": "looks good",
                "final_code": "",
                "files": [
                    {"path": "app.py", "language": "python", "content": "x=1"},
                    {"path": "lib.py", "language": "python", "content": "y=2"},
                ],
            }
        ),
    )
    result = await agents.lead_verdict("spec", "code", "{}")
    assert len(result["files"]) == 2
    assert result["files"][0].path == "app.py"


# ---------------------------------------------------------------------------
# Governor invariant: write_code override + governed chat paths
# ---------------------------------------------------------------------------


def _spy_governor_call(monkeypatch) -> list[str]:
    """Patch agents.governor.call to record providers; make_call still runs."""
    seen: list[str] = []

    async def spy(provider_name, make_call, fallback=None, fallback_factory=None):
        seen.append(provider_name)
        return await make_call()

    monkeypatch.setattr(agents.governor, "call", spy)
    return seen


@pytest.mark.asyncio
async def test_write_code_explicit_provider_skips_quota(monkeypatch):
    quota_called = {"v": False}

    def fake_coder_model():
        quota_called["v"] = True
        return ("zen", "should-not-be-used")

    received = {}

    async def fake_complete(provider, model, user, system="", max_tokens=4096):
        received["provider"] = provider
        received["model"] = model
        return "raw"

    monkeypatch.setattr(agents.quota, "coder_model", fake_coder_model)
    monkeypatch.setattr(agents.llm, "complete", fake_complete)
    monkeypatch.setattr(
        agents.llm,
        "complete_json_obj",
        _make_invoking_fake_json_obj({"language": "python", "code": "x=1", "notes": ""}),
    )
    seen = _spy_governor_call(monkeypatch)

    await agents.write_code("spec", provider="local", model="m1")
    assert seen == ["local"]
    assert received == {"provider": "local", "model": "m1"}
    assert quota_called["v"] is False


@pytest.mark.asyncio
async def test_write_code_without_override_uses_quota(monkeypatch):
    async def fake_complete(provider, model, user, system="", max_tokens=4096):
        return "raw"

    monkeypatch.setattr(agents.llm, "complete", fake_complete)
    monkeypatch.setattr(
        agents.llm,
        "complete_json_obj",
        _make_invoking_fake_json_obj({"language": "python", "code": "x=1", "notes": ""}),
    )
    seen = _spy_governor_call(monkeypatch)

    await agents.write_code("spec")
    # Provider came from the quota profile (default CODER_MODEL -> zen)
    assert seen == ["zen"]


@pytest.mark.asyncio
async def test_lead_regen_artifacts_routes_through_governor(monkeypatch):
    async def fake_complete_history(prov, model, messages, max_tokens=4096):
        return '{"files": [{"path": "a.py", "language": "python", "content": "x"}]}'

    monkeypatch.setattr(agents.llm, "complete_history", fake_complete_history)
    monkeypatch.setattr(
        agents.llm,
        "complete_json_obj",
        _make_invoking_fake_json_obj(
            {"files": [{"path": "a.py", "language": "python", "content": "x"}]}
        ),
    )
    seen = _spy_governor_call(monkeypatch)

    files = await agents.lead_regen_artifacts("sys", [{"role": "user", "content": "hi"}])
    assert len(seen) == 1
    assert files[0].path == "a.py"


@pytest.mark.asyncio
async def test_lead_chat_routes_through_governor(monkeypatch):
    async def fake_complete_history(prov, model, messages, max_tokens=4096):
        return "reply"

    monkeypatch.setattr(agents.llm, "complete_history", fake_complete_history)
    seen = _spy_governor_call(monkeypatch)

    reply = await agents.lead_chat("sys", [{"role": "user", "content": "hi"}])
    assert reply == "reply"
    assert len(seen) == 1


@pytest.mark.asyncio
async def test_lead_chat_stream_anthropic_uses_rpm(monkeypatch):
    """Anthropic streaming cannot retry mid-stream: it must still acquire
    the provider rate-limit slot via governor.rpm."""
    import contextlib

    rpm_used = {"v": False}

    @contextlib.asynccontextmanager
    async def fake_rpm(name):
        rpm_used["v"] = True
        yield

    async def fake_stream(model, history, system=None, max_tokens=4096):
        yield "a"
        yield "b"

    monkeypatch.setattr(agents.quota, "lead_model", lambda: ("anthropic", "claude-x"))
    monkeypatch.setattr(agents.governor, "rpm", fake_rpm)
    monkeypatch.setattr(agents.llm, "call_anthropic_history_stream", fake_stream)

    out = [d async for d in agents.lead_chat_stream("sys", [])]
    assert out == ["a", "b"]
    assert rpm_used["v"] is True


@pytest.mark.asyncio
async def test_lead_chat_stream_non_anthropic_governed(monkeypatch):
    async def fake_complete_history(prov, model, messages, max_tokens=4096):
        return "chunk"

    monkeypatch.setattr(agents.quota, "lead_model", lambda: ("zen", "m"))
    monkeypatch.setattr(agents.llm, "complete_history", fake_complete_history)
    seen = _spy_governor_call(monkeypatch)

    out = [d async for d in agents.lead_chat_stream("sys", [])]
    assert out == ["chunk"]
    assert len(seen) == 1
