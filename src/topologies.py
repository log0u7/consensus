"""Pipeline topologies: orchestration patterns for a Team.

Each topology receives a Team, a spec, and context, then runs the agents
and emits SSE-style events (dicts with a "type" key) via an async generator.

Topologies:
  consensus  coder -> parallel panel -> consensus aggregator -> lead
             (the original pipeline, now driven by team manifests)
  pipeline   planner -> executor -> verifier  (sequential, e.g. SRE/infra)
  loop       recon -> exploit -> report  (iterative, e.g. pentest/CTF)

The application (pipeline.py) selects the topology by name from the team
manifest and delegates; it never hardcodes the flow.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Callable

from . import agents, config, llm, quota
from . import sandbox as sandbox_mod
from .models import ConsensusReport, CostSummary, PipelineResult, SandboxResult, Usage
from .roles import Role, Team

log = logging.getLogger(__name__)


def _summarize(usages: list[Usage]) -> CostSummary:
    cost = sum(u.cost for u in usages if u.cost is not None)
    cost_known = any(u.cost is not None for u in usages)
    return CostSummary(
        calls=len(usages),
        input_tokens=sum(u.input_tokens for u in usages),
        output_tokens=sum(u.output_tokens for u in usages),
        cost=round(cost, 6),
        cost_known=cost_known,
    )


def _panel_members(reviewer_role: Role) -> list[dict]:
    """Build the panel member list from the reviewer Role.

    Prefers role.members (explicit) over config.PANEL (env-driven).
    Falls back to quota.panel() (respects low-quota mode).
    """
    if reviewer_role.members:
        return [
            {
                "name": m["name"],
                "provider": m["model"].split("/", 1)[0] if "/" in m["model"] else "zen",
                "model": m["model"].split("/", 1)[1] if "/" in m["model"] else m["model"],
                "max_tokens": m.get("max_tokens"),
            }
            for m in reviewer_role.members
        ]
    return quota.panel()


# ---------------------------------------------------------------------------
# Topology: consensus  (coder -> panel -> consensus -> lead)
# ---------------------------------------------------------------------------

async def run_consensus(
    team: Team,
    spec: str,
    context: str = "",
    rag_sources: list[dict] | None = None,
    run_id: str = "",
) -> AsyncIterator[dict]:
    """The consensus topology, driven by the team manifest.

    Emits the same event shapes as the original pipeline so the API, UI,
    and TUI are unaffected.
    """
    rag_sources = rag_sources or []
    rlog = lambda lvl, msg, *a: getattr(log, lvl)("[run %s] " + msg, run_id, *a)  # noqa: E731

    with llm.usage_scope() as usages:
        t0 = time.perf_counter()

        coder_role = team.roles.get("coder")
        reviewer_role = team.roles.get("reviewer")

        # Override quota model refs from team manifest when present.
        if coder_role and coder_role.model:
            prov, mod = coder_role.model.split("/", 1)
        else:
            prov, mod = quota.coder_model()

        # 1. Code
        coded = await agents.write_code(spec, context)
        code = coded["code"]
        rlog("info", "coder done (%.1fs)", time.perf_counter() - t0)
        yield {
            "type": "code",
            "code": code,
            "language": coded["language"],
            "usage": _summarize(usages).model_dump(),
        }

        # 1b. Optional sandbox execution (opt-in via coder_role.sandbox)
        exec_result: sandbox_mod.SandboxResult | None = None
        sandbox_context = ""
        if coder_role and coder_role.sandbox and coded["files"]:
            rlog("info", "sandbox: executing generated code")
            import sys as _sys
            _pyexe = _sys.executable
            exec_result = await sandbox_mod.run(
                coded["files"],
                cmd=f"{_pyexe} {coded['files'][0].path}" if coded["files"] else f"{_pyexe} -c 'pass'",
            )
            sandbox_context = exec_result.as_context()
            rlog(
                "info", "sandbox done: exit=%d timed_out=%s",
                exec_result.exit_code, exec_result.timed_out,
            )
            yield {
                "type": "execution",
                "execution": {
                    "stdout": exec_result.stdout,
                    "stderr": exec_result.stderr,
                    "exit_code": exec_result.exit_code,
                    "timed_out": exec_result.timed_out,
                    "engine": exec_result.engine,
                },
                "usage": _summarize(usages).model_dump(),
            }

        # Inject execution output into code context for panel.
        panel_code = code if not sandbox_context else f"{code}\n\n{sandbox_context}"

        # 2. Panel (parallel, resilient)
        members = _panel_members(reviewer_role) if reviewer_role else quota.panel()
        t_panel = time.perf_counter()
        reviews: list = []
        tasks = [asyncio.create_task(agents.review_code(m, panel_code)) for m in members]
        for fut in asyncio.as_completed(tasks):
            review = await fut
            reviews.append(review)
            yield {
                "type": "review",
                "review": review.model_dump(),
                "usage": _summarize(usages).model_dump(),
            }
        rlog(
            "info", "panel done (%.1fs): %d/%d answered",
            time.perf_counter() - t_panel,
            sum(1 for r in reviews if r.ok), len(reviews),
        )

        # 3. Consensus
        consensus: ConsensusReport = await agents.build_consensus(reviews)
        yield {
            "type": "consensus",
            "consensus": consensus.model_dump(),
            "usage": _summarize(usages).model_dump(),
        }

        # 4. Lead verdict
        verdict = await agents.lead_verdict(spec, code, consensus.model_dump_json())
        files = verdict["files"] or coded["files"]

        summary = _summarize(usages)
        rlog(
            "info", "run done (%.1fs): %d calls, %d/%d tokens, cost=%s",
            time.perf_counter() - t0, summary.calls,
            summary.input_tokens, summary.output_tokens,
            str(summary.cost) if summary.cost_known else "unknown",
        )

        exec_model: SandboxResult | None = None
        if exec_result is not None:
            exec_model = SandboxResult(
                stdout=exec_result.stdout,
                stderr=exec_result.stderr,
                exit_code=exec_result.exit_code,
                timed_out=exec_result.timed_out,
                skipped=exec_result.skipped,
                engine=exec_result.engine,
            )

        yield {
            "type": "result",
            "result": PipelineResult(
                spec=spec,
                code=code,
                language=coded["language"],
                reviews=reviews,
                consensus=consensus,
                verdict=verdict["verdict"],
                final_code=verdict["final_code"],
                rationale=verdict["rationale"],
                files=files,
                rag_sources=rag_sources,
                execution=exec_model,
                usages=list(usages),
                cost_summary=summary,
            ).model_dump(),
        }


# ---------------------------------------------------------------------------
# Topology: pipeline  (planner -> executor -> verifier, sequential)
# ---------------------------------------------------------------------------

async def run_pipeline(
    team: Team,
    spec: str,
    context: str = "",
    rag_sources: list[dict] | None = None,
    run_id: str = "",
) -> AsyncIterator[dict]:
    """Sequential pipeline topology: planner -> executor -> verifier.

    Each role receives the output of the previous one as context.
    Emits {"type": "step", "role": name, "output": str, "usage": ...} per step,
    then a final {"type": "result", "result": ...}.
    """
    rag_sources = rag_sources or []
    rlog = lambda lvl, msg, *a: getattr(log, lvl)("[run %s] " + msg, run_id, *a)  # noqa: E731
    role_names = list(team.roles.keys())

    with llm.usage_scope() as usages:
        t0 = time.perf_counter()
        accumulated = context
        outputs: dict[str, str] = {}

        for role_name in role_names:
            role = team.roles[role_name]
            prov, mod = role.model.split("/", 1) if "/" in role.model else quota.coder_model()
            user = f"Task: {spec}\n\nContext so far:\n{accumulated}" if accumulated else f"Task: {spec}"

            tok = llm.set_step(role_name)
            try:
                output = await llm.complete(prov, mod, user, max_tokens=role.max_tokens or config.CODER_MAX_TOKENS)
            finally:
                llm.reset_step(tok)

            outputs[role_name] = output
            accumulated = f"{accumulated}\n\n[{role_name}]:\n{output}".strip()
            rlog("info", "step %s done (%.1fs)", role_name, time.perf_counter() - t0)
            yield {
                "type": "step",
                "role": role_name,
                "output": output,
                "usage": _summarize(usages).model_dump(),
            }

        summary = _summarize(usages)
        yield {
            "type": "result",
            "result": {
                "spec": spec,
                "topology": "pipeline",
                "outputs": outputs,
                "rag_sources": rag_sources,
                "cost_summary": summary.model_dump(),
            },
        }


# ---------------------------------------------------------------------------
# Topology: loop  (recon -> exploit -> report, iterative)
# ---------------------------------------------------------------------------

async def run_loop(
    team: Team,
    spec: str,
    context: str = "",
    rag_sources: list[dict] | None = None,
    run_id: str = "",
    max_iterations: int = 3,
) -> AsyncIterator[dict]:
    """Iterative loop topology: cycles through all roles up to max_iterations.

    Stops early when any role emits a response containing '[DONE]'.
    Emits {"type": "iteration", "i": n, "role": name, "output": str, "usage": ...},
    then a final {"type": "result", ...}.
    """
    rag_sources = rag_sources or []
    rlog = lambda lvl, msg, *a: getattr(log, lvl)("[run %s] " + msg, run_id, *a)  # noqa: E731
    role_names = list(team.roles.keys())

    with llm.usage_scope() as usages:
        t0 = time.perf_counter()
        accumulated = context
        outputs: dict[str, list[str]] = {r: [] for r in role_names}
        done = False

        for i in range(1, max_iterations + 1):
            rlog("info", "loop iteration %d", i)
            for role_name in role_names:
                role = team.roles[role_name]
                prov, mod = role.model.split("/", 1) if "/" in role.model else quota.coder_model()
                user = (
                    f"Iteration {i}. Task: {spec}\n\nContext so far:\n{accumulated}"
                    if accumulated else f"Iteration {i}. Task: {spec}"
                )
                tok = llm.set_step(f"{role_name}:{i}")
                try:
                    output = await llm.complete(prov, mod, user, max_tokens=role.max_tokens or config.CODER_MAX_TOKENS)
                finally:
                    llm.reset_step(tok)

                outputs[role_name].append(output)
                accumulated = f"{accumulated}\n\n[{role_name} i={i}]:\n{output}".strip()
                yield {
                    "type": "iteration",
                    "i": i,
                    "role": role_name,
                    "output": output,
                    "usage": _summarize(usages).model_dump(),
                }
                if "[DONE]" in output:
                    done = True
                    break
            if done:
                break

        summary = _summarize(usages)
        rlog("info", "loop done (%.1fs)", time.perf_counter() - t0)
        yield {
            "type": "result",
            "result": {
                "spec": spec,
                "topology": "loop",
                "outputs": outputs,
                "rag_sources": rag_sources,
                "cost_summary": summary.model_dump(),
            },
        }


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

_TopologyFn = Callable[..., AsyncIterator[dict]]

_REGISTRY: dict[str, _TopologyFn] = {
    "consensus": run_consensus,  # type: ignore[dict-item]
    "pipeline":  run_pipeline,   # type: ignore[dict-item]
    "loop":      run_loop,       # type: ignore[dict-item]
}


async def run(
    team: Team,
    spec: str,
    context: str = "",
    rag_sources: list[dict] | None = None,
    run_id: str = "",
) -> AsyncIterator[dict]:
    """Dispatch to the topology named in team.topology."""
    fn = _REGISTRY.get(team.topology)
    if fn is None:
        raise ValueError(
            f"Unknown topology {team.topology!r}. "
            f"Available: {list(_REGISTRY)}"
        )
    return fn(team, spec, context=context, rag_sources=rag_sources, run_id=run_id)
