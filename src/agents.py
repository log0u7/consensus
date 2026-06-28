"""Agent functions.

Each builds a prompt, calls the right model via llm.complete(), and returns
structured data.  No framework: prompts request strict JSON, llm.complete_json_obj
recovers it with repair and retry.
"""

import logging

from pydantic import ValidationError

from . import config, llm, quota
from .models import (
    Artifact,
    ConsensusIssue,
    ConsensusReport,
    Issue,
    Review,
)

log = logging.getLogger(__name__)


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


async def write_code(spec: str, context: str = "") -> dict:
    user = spec if not context else f"Internal context:\n{context}\n\nTask:\n{spec}"
    provider, model = quota.coder_model()
    tok = llm.set_step("coder")
    try:
        data = await llm.complete_json_obj(
            lambda attempt: llm.complete(
                provider, model,
                user + (llm._JSON_RETRY_HINT if attempt else ""),
                _CODER_SYS,
                max_tokens=config.CODER_MAX_TOKENS,
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


async def review_code(panel_member: dict, code: str) -> Review:
    """Run one reviewer. On failure, return Review(ok=False) so the panel
    stays resilient (consensus uses whoever answered)."""
    name = panel_member["name"]
    provider = panel_member["provider"]
    model = panel_member["model"]
    max_tokens = panel_member.get("max_tokens") or config.REVIEW_MAX_TOKENS
    user = f"Code to review:\n\n{code}"
    tok = llm.set_step(f"reviewer:{name}")
    try:
        data = await llm.complete_json_obj(
            lambda attempt: llm.complete(
                provider, model,
                user + (llm._JSON_RETRY_HINT if attempt else ""),
                _REVIEW_SYS,
                max_tokens=max_tokens,
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


async def build_consensus(reviews: list[Review]) -> ConsensusReport:
    participating = [r for r in reviews if r.ok]
    panel_names = [r.reviewer for r in participating]
    n = max(len(participating), 1)

    if not participating:
        return ConsensusReport(panel=[], issues=[], summary="No reviewer answered.")

    blob_parts = []
    for r in participating:
        lines = [f"### Reviewer: {r.reviewer}", f"overall: {r.overall}"]
        for i in r.issues:
            lines.append(
                f"- [{i.severity}/{i.category}] {i.title} @ {i.location}: {i.description}"
            )
        blob_parts.append("\n".join(lines))
    blob = "\n\n".join(blob_parts)

    provider, model = quota.consensus_model()
    tok = llm.set_step("consensus")
    try:
        data = await llm.complete_json_obj(
            lambda attempt: llm.complete(
                provider, model,
                f"Reviews:\n\n{blob}" + (llm._JSON_RETRY_HINT if attempt else ""),
                _CONSENSUS_SYS,
                max_tokens=config.CONSENSUS_MAX_TOKENS,
            )
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
                unknown, panel_names,
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


async def lead_verdict(spec: str, code: str, consensus_json: str) -> dict:
    system = LEAD_SYSTEM_TEMPLATE.format(spec=spec, code=code, consensus=consensus_json)
    provider, model = quota.lead_model()
    tok = llm.set_step("lead")

    def _budget(attempt: int) -> int:
        return min(config.LEAD_MAX_TOKENS * (attempt + 1), 64000)

    try:
        data = await llm.complete_json_obj(
            lambda attempt: llm.complete(
                provider, model,
                _LEAD_VERDICT_INSTR + (llm._JSON_RETRY_HINT if attempt else ""),
                system,
                max_tokens=_budget(attempt),
            )
        )
    except ValueError as exc:
        log.warning("lead verdict unparseable, returning degraded verdict: %s", exc)
        return {
            "verdict": "APPROVE_WITH_CHANGES",
            "rationale": (
                "The Lead's structured answer could not be parsed (likely truncated). "
                "Review the panel and consensus, then ask the Lead in the chat to "
                "restate its verdict or regenerate the files."
            ),
            "final_code": "",
            "files": [],
        }
    finally:
        llm.reset_step(tok)

    return {
        "verdict": data.get("verdict", ""),
        "rationale": data.get("rationale", ""),
        "final_code": data.get("final_code", ""),
        "files": _parse_files(data.get("files")),
    }


async def lead_regen_artifacts(system: str, history: list[dict[str, str]]) -> list[Artifact]:
    """Ask the Lead to emit the current project as a file tree."""
    provider, model = quota.lead_model()
    tok = llm.set_step("chat")
    try:
        data = await llm.complete_json_obj(
            lambda attempt: llm.complete(
                provider, model,
                (_LEAD_REGEN_INSTR + llm._JSON_RETRY_HINT) if attempt
                else _LEAD_REGEN_INSTR,
                system,
                max_tokens=config.LEAD_MAX_TOKENS,
            )
        )
    finally:
        llm.reset_step(tok)
    return _parse_files(data.get("files"))


async def lead_chat(system: str, history: list[dict[str, str]]) -> str:
    """Free-form conversation with the Lead."""
    provider, model = quota.lead_model()
    if provider == "anthropic":
        return await llm.call_anthropic_history(model, history, system=system,
                                                max_tokens=config.CHAT_MAX_TOKENS)
    return await llm.complete(provider, model,
                              history[-1]["content"] if history else "",
                              system, config.CHAT_MAX_TOKENS)


def lead_chat_stream(system: str, history: list[dict[str, str]]):
    """Streaming variant of lead_chat: async iterator of text deltas.
    Falls back gracefully if the provider does not support native streaming."""
    provider, model = quota.lead_model()
    if provider == "anthropic":
        return llm.call_anthropic_history_stream(model, history, system=system,
                                                 max_tokens=config.CHAT_MAX_TOKENS)
    # Non-Anthropic providers: wrap the single call as an async generator
    async def _wrap():
        result = await llm.complete(provider, model,
                                    history[-1]["content"] if history else "",
                                    system, config.CHAT_MAX_TOKENS)
        yield result
    return _wrap()
