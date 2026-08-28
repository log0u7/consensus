"""Agent functions.

Each builds a prompt, calls the right model via llm.complete(), and returns
structured data.  No framework: prompts request strict JSON, llm.complete_json_obj
recovers it with repair and retry.
"""

import logging
from collections.abc import Awaitable

from pydantic import ValidationError

from . import config, governor, llm, quota
from .models import (
    Artifact,
    ConsensusIssue,
    ConsensusReport,
    Issue,
    Review,
)

log = logging.getLogger(__name__)


def context_stats(system: str, user: str, provider: str, model: str) -> dict:
    """Describe prompt size for an event's `context` field.

    Token counts are rough (chars / 4); the topology enriches them with
    pricing-derived context_window / est_input_cost.
    """
    st = len(system) // 4
    ut = len(user) // 4
    return {
        "system_tokens": st,
        "user_tokens": ut,
        "total_tokens": st + ut,
        "model": f"{provider}/{model}",
    }


def _parse_files(raw: object) -> list[Artifact]:
    """Validate a model-provided files list into Artifacts, skipping invalid entries."""
    out: list[Artifact] = []
    if not isinstance(raw, list):
        return out
    for item in raw:
        try:
            out.append(Artifact.model_validate(item))
        except (ValidationError, ValueError) as exc:
            log.warning("skipping invalid artifact %r: %s", item, exc)
    return out


def files_to_blob(files: list[Artifact]) -> str:
    """Concatenate a multi-file solution into a single reviewable blob."""
    return "\n\n".join(f"# ===== {f.path} =====\n{f.content}" for f in files)


async def governed_call(
    provider: str,
    model: str,
    user: str,
    system: str | None = None,
    max_tokens: int = 4096,
    fallback: list[str] | None = None,
) -> str:
    """One LLM call routed through the governor (rate-limit + retry + fallback).

    Single source of truth for topology role steps; agents must never call
    llm.complete() directly.
    """

    def _make(prov: str) -> Awaitable[str]:
        return llm.complete(prov, model, user, system=system, max_tokens=max_tokens)

    return await governor.call(
        provider,
        lambda: _make(provider),
        fallback=fallback,
        fallback_factory=lambda p: lambda: _make(p),
    )


# ---------------------------------------------------------------------------
# Coder
# ---------------------------------------------------------------------------

_CODER_SYS = (
    "You are a senior software engineer. Produce clean, correct, idiomatic "
    "code that fulfils the specification. Comments in English. Return ONLY a "
    "JSON object, no markdown fences. For a single-file solution use keys "
    '{"language": str, "code": str, "notes": str}. For a multi-file project '
    "(idiomatically split across files) instead use keys "
    '{"language": str, "files": [{"path": str, "language": str, '
    '"content": str}], "notes": str}, where path is a RELATIVE path (no '
    "leading slash, no '..'). Choose multi-file only when it is the idiomatic "
    "layout; otherwise keep a single file."
)


async def write_code(
    spec: str,
    context: str = "",
    provider: str | None = None,
    model: str | None = None,
) -> dict:
    """Coder step. provider/model override the quota profile (team manifest);
    when either is missing the quota profile decides."""
    user = spec if not context else f"Internal context:\n{context}\n\nTask:\n{spec}"
    if provider is None or model is None:
        provider, model = quota.coder_model()
    tok = llm.set_step("coder")
    try:

        def _make(prov: str, attempt: int) -> Awaitable[str]:
            return llm.complete(
                prov,
                model,
                user + (llm._JSON_RETRY_HINT if attempt else ""),
                _CODER_SYS,
                max_tokens=config.CODER_MAX_TOKENS,
            )

        data = await llm.complete_json_obj(
            lambda attempt: governor.call(
                provider,
                lambda: _make(provider, attempt),
                fallback=config.CODER_FALLBACK,
                fallback_factory=lambda p: lambda: _make(p, attempt),
            )
        )
    finally:
        llm.reset_step(tok)
    files = _parse_files(data.get("files"))
    code = data.get("code", "")
    if files and not code:
        code = files_to_blob(files)
    return {
        "language": data.get("language", ""),
        "code": code,
        "notes": data.get("notes", ""),
        "files": files,
        "context_stats": context_stats(
            _CODER_SYS,
            user,
            provider,
            model,
        ),
    }


# ---------------------------------------------------------------------------
# Reviewer (one panel member)
# ---------------------------------------------------------------------------

_REVIEW_SYS = (
    "You are a ruthless QA and security reviewer. Review the given code "
    "independently. Find real defects: security flaws, correctness bugs, race "
    "conditions, resource leaks, error handling gaps, performance traps. Do "
    "not invent issues to look thorough. Return ONLY a JSON object, no markdown "
    "fences, with keys: "
    '{"issues": [{"title": str, "severity": "critical|high|medium|low", '
    '"category": "security|correctness|performance|style|maintainability", '
    '"location": str, "description": str}], "overall": str}.'
)


def _review_user(code: str) -> str:
    return f"Code to review:\n\n{code}"


def review_context_stats(panel_member: dict, code: str) -> dict:
    """Context stats for one reviewer call (same prompt as review_code)."""
    return context_stats(
        _REVIEW_SYS,
        _review_user(code),
        panel_member["provider"],
        panel_member["model"],
    )


async def review_code(panel_member: dict, code: str) -> Review:
    """Run one reviewer. On failure, return Review(ok=False) so the panel
    stays resilient (consensus uses whoever answered)."""
    name = panel_member["name"]
    provider = panel_member["provider"]
    model = panel_member["model"]
    max_tokens = panel_member.get("max_tokens") or config.REVIEW_MAX_TOKENS
    user = _review_user(code)
    tok = llm.set_step(f"reviewer:{name}")
    try:

        def _make(prov: str, attempt: int) -> Awaitable[str]:
            return llm.complete(
                prov,
                model,
                user + (llm._JSON_RETRY_HINT if attempt else ""),
                _REVIEW_SYS,
                max_tokens=max_tokens,
            )

        data = await llm.complete_json_obj(
            lambda attempt: governor.call(
                provider,
                lambda: _make(provider, attempt),
                fallback=config.REVIEWER_FALLBACK,
                fallback_factory=lambda p: lambda: _make(p, attempt),
            )
        )
        issues = []
        for i in data.get("issues", []):
            try:
                issues.append(Issue.model_validate(i))
            except ValidationError as exc:
                log.warning("reviewer %s produced an invalid issue, skipping: %s", name, exc)
        return Review(reviewer=name, ok=True, issues=issues, overall=data.get("overall", ""))
    except Exception as exc:  # noqa: BLE001 - resilience is the point
        log.warning("reviewer %s failed: %s: %s", name, type(exc).__name__, exc)
        return Review(reviewer=name, ok=False, error=f"{type(exc).__name__}: {exc}")
    finally:
        llm.reset_step(tok)


# ---------------------------------------------------------------------------
# Consensus aggregator
# ---------------------------------------------------------------------------

_CONSENSUS_SYS = (
    "You merge several independent code reviews into one consensus report. "
    "Each review comes from a different model. Match issues that are the SAME "
    "problem across reviewers by meaning, not exact wording. For each distinct "
    "issue list which reviewers flagged it. Resolve severity disagreements by "
    "the most common severity. An issue flagged by many reviewers is high "
    "confidence; an issue flagged by a single reviewer is low confidence and "
    "may be a false positive. Return ONLY a JSON object, no markdown fences, "
    'with keys: {"issues": [{"title": str, "severity": str, "category": str, '
    '"description": str, "flagged_by": [reviewer_name, ...]}], "summary": str}.'
)


def _reviews_blob(reviews: list[Review]) -> str:
    participating = [r for r in reviews if r.ok]
    blob_parts = []
    for r in participating:
        lines = [f"### Reviewer: {r.reviewer}", f"overall: {r.overall}"]
        for i in r.issues:
            lines.append(f"- [{i.severity}/{i.category}] {i.title} @ {i.location}: {i.description}")
        blob_parts.append("\n".join(lines))
    return "\n\n".join(blob_parts)


def consensus_context_stats(reviews: list[Review]) -> dict:
    """Context stats for the consensus aggregation call."""
    provider, model = quota.consensus_model()
    return context_stats(_CONSENSUS_SYS, f"Reviews:\n\n{_reviews_blob(reviews)}", provider, model)


async def build_consensus(reviews: list[Review]) -> ConsensusReport:
    participating = [r for r in reviews if r.ok]
    panel_names = [r.reviewer for r in participating]
    n = max(len(participating), 1)

    if not participating:
        return ConsensusReport(panel=[], issues=[], summary="No reviewer answered.")

    blob = _reviews_blob(reviews)

    provider, model = quota.consensus_model()
    tok = llm.set_step("consensus")
    try:

        def _make(prov: str, attempt: int) -> Awaitable[str]:
            return llm.complete(
                prov,
                model,
                f"Reviews:\n\n{blob}" + (llm._JSON_RETRY_HINT if attempt else ""),
                _CONSENSUS_SYS,
                max_tokens=config.CONSENSUS_MAX_TOKENS,
            )

        data = await llm.complete_json_obj(
            lambda attempt: governor.call(
                provider,
                lambda: _make(provider, attempt),
                fallback=config.CONSENSUS_FALLBACK,
                fallback_factory=lambda p: lambda: _make(p, attempt),
            )
        )
    except Exception as exc:
        # Provider failure (429 bursts exhausted, network, ...): the run must
        # always complete, so degrade the consensus and let the Lead arbitrate
        # on the raw reviews alone.
        log.warning("consensus unavailable (%s: %s), degrading report", type(exc).__name__, exc)
        return ConsensusReport(
            panel=panel_names,
            issues=[],
            summary=(
                f"Consensus aggregation failed ({type(exc).__name__}); "
                "the Lead arbitrates on the raw panel reviews below."
            ),
        )
    finally:
        llm.reset_step(tok)

    valid_names = set(panel_names)
    issues = []
    for it in data.get("issues", []):
        # The consensus_score is the core signal: never trust the model's
        # flagged_by blindly. Validate against the real panel, dedupe, derive
        # the score in code, clamp to 1.0.
        raw_flagged = it.get("flagged_by", []) or []
        unknown = [r for r in raw_flagged if r not in valid_names]
        if unknown:
            log.warning(
                "consensus flagged_by referenced unknown reviewers %s (panel=%s)",
                unknown,
                panel_names,
            )
        flagged = sorted({r for r in raw_flagged if r in valid_names})
        score = round(min(len(flagged), n) / n, 3)
        try:
            issues.append(
                ConsensusIssue.model_validate(
                    {
                        "title": it.get("title", ""),
                        "severity": it.get("severity", "medium"),
                        "category": it.get("category", "correctness"),
                        "description": it.get("description", ""),
                        "flagged_by": flagged,
                        "consensus_score": score,
                    }
                )
            )
        except ValidationError as exc:
            log.warning("consensus produced an invalid issue, skipping: %s", exc)

    issues.sort(key=lambda x: (-x.consensus_score, _sev_rank(x.severity)))
    return ConsensusReport(panel=panel_names, issues=issues, summary=data.get("summary", ""))


def _sev_rank(sev: str) -> int:
    return {"critical": 0, "high": 1, "medium": 2, "low": 3}.get(sev.lower(), 4)


# ---------------------------------------------------------------------------
# Lead (verdict + final code + conversational chat)
# ---------------------------------------------------------------------------

LEAD_SYSTEM_TEMPLATE = (
    "You are the Tech Lead and final arbiter. You receive an original spec, "
    "the code a developer produced, and a CONSENSUS review report from a panel "
    "of independent models. High consensus_score means many reviewers agreed "
    "(trust it); low score means a single reviewer raised it (treat as a "
    "candidate, judge on merit). Be decisive.\n\n"
    "=== SPEC ===\n{spec}\n\n"
    "=== CODE ===\n{code}\n\n"
    "=== CONSENSUS REPORT (JSON) ===\n{consensus}\n\n"
    "After your first answer you will keep talking with the developer about "
    "this code as a normal chat. Always answer in the developer's language."
)

_LEAD_VERDICT_INSTR = (
    "Give your initial decision now. Return ONLY a JSON object, no markdown "
    "fences, with keys: "
    '{"verdict": "APPROVE|APPROVE_WITH_CHANGES|REJECT", '
    '"rationale": str, "final_code": str, '
    '"files": [{"path": str, "language": str, "content": str}]}. '
    "final_code must be the corrected, deployable version addressing the "
    "high-consensus issues. If the solution is idiomatically multiple files, "
    "put them in `files` with RELATIVE paths and leave final_code empty; "
    "otherwise omit `files` and use final_code."
)

_LEAD_REGEN_INSTR = (
    "Based on the current state of this project as discussed, output the "
    "complete file tree now. Return ONLY a JSON object, no markdown fences, "
    'with keys: {"files": [{"path": str, "language": str, "content": str}]}. '
    "Paths are RELATIVE (no leading slash, no '..'). Include every file needed "
    "to run the solution."
)


def _degraded_verdict(code: str, reason: str) -> dict:
    """Verdict used when the Lead itself could not answer. Never empty:
    final_code carries the coder's code so the UI/report always show code."""
    return {
        "verdict": "APPROVE_WITH_CHANGES",
        "degraded": True,
        "rationale": (
            f"{reason} Review the panel and consensus, then ask the Lead in "
            "the chat to restate its verdict or regenerate the files."
        ),
        "final_code": code,
        "files": [],
    }


async def lead_verdict(spec: str, code: str, consensus_json: str) -> dict:
    system = LEAD_SYSTEM_TEMPLATE.format(spec=spec, code=code, consensus=consensus_json)
    provider, model = quota.lead_model()
    tok = llm.set_step("lead")

    def _budget(attempt: int) -> int:
        return min(config.LEAD_MAX_TOKENS * (attempt + 1), 64000)

    try:

        def _make(prov: str, attempt: int) -> Awaitable[str]:
            return llm.complete(
                prov,
                model,
                _LEAD_VERDICT_INSTR + (llm._JSON_RETRY_HINT if attempt else ""),
                system,
                max_tokens=_budget(attempt),
            )

        data = await llm.complete_json_obj(
            lambda attempt: governor.call(
                provider,
                lambda: _make(provider, attempt),
                fallback=config.LEAD_FALLBACK,
                fallback_factory=lambda p: lambda: _make(p, attempt),
            )
        )
    except ValueError as exc:
        log.warning("lead verdict unparseable, returning degraded verdict: %s", exc)
        return _degraded_verdict(
            code, "The Lead's structured answer could not be parsed (likely truncated)."
        )
    except Exception as exc:
        # Provider failure (429 bursts exhausted, network, ...): degrade
        # instead of killing the run - the panel output stays reviewable.
        log.warning(
            "lead call failed (%s: %s), returning degraded verdict", type(exc).__name__, exc
        )
        return _degraded_verdict(code, f"The Lead is unreachable ({type(exc).__name__}).")
    finally:
        llm.reset_step(tok)

    return {
        "verdict": data.get("verdict", ""),
        "degraded": False,
        "rationale": data.get("rationale", ""),
        "final_code": data.get("final_code", ""),
        "files": _parse_files(data.get("files")),
        "context_stats": context_stats(system, _LEAD_VERDICT_INSTR, provider, model),
    }


async def lead_regen_artifacts(system: str, history: list[dict[str, str]]) -> list[Artifact]:
    """Ask the Lead to emit the current project as a file tree.

    The full conversation history is passed so the Lead has context of any
    changes discussed before being asked to regenerate the files.
    """
    provider, model = quota.lead_model()
    tok = llm.set_step("chat")
    try:

        def _make(prov: str, attempt: int) -> Awaitable[str]:
            instr = _LEAD_REGEN_INSTR + (llm._JSON_RETRY_HINT if attempt else "")
            messages = _with_system(system, list(history) + [{"role": "user", "content": instr}])
            return llm.complete_history(prov, model, messages, config.LEAD_MAX_TOKENS)

        data = await llm.complete_json_obj(
            lambda attempt: governor.call(
                provider,
                lambda: _make(provider, attempt),
                fallback=config.LEAD_FALLBACK,
                fallback_factory=lambda p: lambda: _make(p, attempt),
            )
        )
    finally:
        llm.reset_step(tok)
    return _parse_files(data.get("files"))


async def lead_chat(system: str, history: list[dict[str, str]]) -> str:
    """Free-form conversation with the Lead (full history, all transports).

    Uses the chat profile (CHAT_MODEL, e.g. a local model for exploration);
    falls back to the Lead model when CHAT_MODEL is unset."""
    provider, model = quota.chat_model()

    def _make(prov: str) -> Awaitable[str]:
        # Anthropic transport takes system as a top-level parameter;
        # openai-compatible transports embed it as the first message.
        if prov == "anthropic":
            return llm.call_anthropic_history(
                model, history, system=system, max_tokens=config.CHAT_MAX_TOKENS
            )
        return llm.complete_history(
            prov, model, _with_system(system, history), config.CHAT_MAX_TOKENS
        )

    return await governor.call(
        provider,
        lambda: _make(provider),
        fallback=config.LEAD_FALLBACK,
        fallback_factory=lambda p: lambda: _make(p),
    )


def lead_chat_stream(system: str, history: list[dict[str, str]]):
    """Streaming variant of lead_chat: async iterator of text deltas.
    Anthropic uses native SSE streaming; other providers emit one chunk.
    Streaming cannot retry mid-stream, so the Anthropic path only acquires
    the provider's rate-limit slot (governor.rpm).
    Uses the chat profile (CHAT_MODEL) like lead_chat."""
    provider, model = quota.chat_model()
    if provider == "anthropic":

        async def _anthropic_stream():
            async with governor.rpm("anthropic"):
                async for delta in llm.call_anthropic_history_stream(
                    model, history, system=system, max_tokens=config.CHAT_MAX_TOKENS
                ):
                    yield delta

        return _anthropic_stream()

    # Non-Anthropic: full history through the governor, wrapped as a generator.
    messages = _with_system(system, history)

    def _make(prov: str) -> Awaitable[str]:
        return llm.complete_history(prov, model, messages, config.CHAT_MAX_TOKENS)

    async def _wrap():
        result = await governor.call(
            provider,
            lambda: _make(provider),
            fallback=config.LEAD_FALLBACK,
            fallback_factory=lambda p: lambda: _make(p),
        )
        yield result

    return _wrap()


def _with_system(system: str, history: list[dict[str, str]]) -> list[dict[str, str]]:
    """Prepend a system message to a history list for openai-compatible providers."""
    if not system:
        return history
    return [{"role": "system", "content": system}, *history]
