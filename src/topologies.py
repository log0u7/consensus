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

from . import agents, config, llm, pricing, providers, quota
from . import sandbox as sandbox_mod
from .agents import ToolRuntime
from .context import build as build_context
from .mcp_client import MCPClientManager
from .models import ConsensusReport, PipelineResult, SandboxResult, summarize_usage
from .roles import Role, Team

log = logging.getLogger(__name__)


def _rag_context_text(hits: list[dict]) -> str:
    """Compact RAG text for the 'Internal context' block (pipeline format)."""
    return "\n\n".join(f"[{h['source']}]\n{h['content']}" for h in hits)


def _context_event(stats: dict) -> dict:
    """Enrich agent-reported context stats with pricing-derived metadata:
    the model's context window and the estimated input cost of one prompt."""
    out = dict(stats)
    ref = stats.get("model", "")
    prov, _, mod = ref.partition("/")
    out["context_window"] = pricing.context_window_for(ref) if ref else None
    out["est_input_cost"] = pricing.cost_for(prov, mod, stats.get("total_tokens", 0), 0)
    return out


def _panel_members(reviewer_role: Role) -> list[dict]:
    """Build the panel member list from the reviewer Role.

    Prefers role.members (explicit) over config.PANEL (env-driven).
    Falls back to quota.panel() (respects low-quota mode).
    """
    if reviewer_role.members:
        return [
            {
                "name": m["name"],
                "provider": providers.resolve_name(m["model"])[0] if "/" in m["model"] else "zen",
                "model": m["model"].split("/", 1)[1] if "/" in m["model"] else m["model"],
                "max_tokens": m.get("max_tokens"),
            }
            for m in reviewer_role.members
        ]
    return quota.panel()


def role_servers(team: Team, role: Role) -> list[dict]:
    """Resolve a role's tool references (server NAMES) to MCP server configs.

    role.tools holds server names defined in team.mcp_servers; unknown names
    are logged and skipped (resilient by default, like all team plumbing).
    """
    if not role.tools:
        return []
    by_name = {s.get("name"): s for s in team.mcp_servers}
    out = []
    for name in role.tools:
        cfg = by_name.get(name)
        if cfg is None:
            log.warning(
                "role %r references MCP server %r not defined in team %r mcp_servers",
                role.name,
                name,
                team.name,
            )
            continue
        out.append(cfg)
    return out


def _runtime_for_role(team: Team, role: Role) -> ToolRuntime | None:
    """Build a ToolRuntime for a role when it references MCP servers.

    Returns None when the role declares no tools or none resolve to a
    configured server (MCPClientManager is then never instantiated, so the
    mcp SDK is never imported without tools).

    ponytail: one MCPClientManager per tool call (connect/call/close) instead
    of a run-scoped manager; if session startup dominates latency, hold one
    manager open for the topology run instead.
    """
    servers = role_servers(team, role)
    if not servers:
        return None

    async def call(name: str, arguments: dict) -> str:
        async with MCPClientManager(servers) as mgr:
            return await mgr.call_tool(name, arguments)

    return ToolRuntime(definitions=[], call=call)


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
        consensus_role = team.roles.get("consensus")
        lead_role = team.roles.get("lead")

        # Declarative context (team manifest): skills/tools -> system prefix,
        # role-level rag_ns -> coder context. Pipeline pre-fetched RAG (if any)
        # is reused; a rag_ns hit list is formatted pipeline-style.
        coder_system_extra = ""
        if coder_role and (coder_role.skills or coder_role.rag_ns):
            ctx = await build_context(spec=spec, role=coder_role, rag_hits=rag_sources or None)
            coder_system_extra = ctx.system
            rag_sources = ctx.rag_sources or rag_sources
            if ctx.rag_sources and not context:
                context = _rag_context_text(ctx.rag_sources)

        # 1. Code (team manifest may pin the coder model; otherwise quota decides).
        # With declared tools, an MCP manager stays open for the coder call:
        # definitions are listed once (system prompt) and call_tool is bound.
        coder_fallback = coder_role.fallback or None if coder_role else None
        coder_servers = role_servers(team, coder_role) if coder_role else []
        coder_runtime: ToolRuntime | None = None
        mgr: MCPClientManager | None = None
        if coder_servers:
            mgr = MCPClientManager(coder_servers)
            await mgr.__aenter__()
            coder_runtime = ToolRuntime(definitions=await mgr.list_tools(), call=mgr.call_tool)
        try:
            if coder_role and coder_role.model:
                prov, mod = providers.resolve_name(coder_role.model)
                coded = await agents.write_code(
                    spec,
                    context,
                    provider=prov,
                    model=mod,
                    system_extra=coder_system_extra,
                    fallback=coder_fallback,
                    tools=coder_runtime,
                )
            else:
                coded = await agents.write_code(
                    spec,
                    context,
                    system_extra=coder_system_extra,
                    fallback=coder_fallback,
                    tools=coder_runtime,
                )
        finally:
            if mgr is not None:
                await mgr.__aexit__(None, None, None)
        code = coded["code"]
        rlog("info", "coder done (%.1fs)", time.perf_counter() - t0)
        # Panel members resolved BEFORE the code event so the UI knows the
        # exact reviewer count (the reactive low-quota panel can differ from
        # the one a cached /api/health snapshot reported).
        members = _panel_members(reviewer_role) if reviewer_role else quota.panel()
        code_event: dict = {
            "type": "code",
            "code": code,
            "language": coded["language"],
            "panel_size": len(members),
            "usage": summarize_usage(usages).model_dump(),
        }
        if coded.get("context_stats"):
            code_event["context"] = _context_event(coded["context_stats"])
        yield code_event

        # 1b. Optional sandbox execution (opt-in via coder_role.sandbox)
        exec_result: sandbox_mod.SandboxResult | None = None
        sandbox_context = ""
        if coder_role and coder_role.sandbox and coded["files"]:
            rlog("info", "sandbox: executing generated code")
            exec_result = await sandbox_mod.run(
                coded["files"],
                cmd=f"{sandbox_mod.SANDBOX_PYTHON} {coded['files'][0].path}",
            )
            if exec_result.skipped:
                rlog(
                    "warning",
                    "sandbox requested but skipped (engine=%s): generated code was NOT executed",
                    exec_result.engine,
                )
            sandbox_context = exec_result.as_context()
            rlog(
                "info",
                "sandbox done: exit=%d timed_out=%s",
                exec_result.exit_code,
                exec_result.timed_out,
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
                "usage": summarize_usage(usages).model_dump(),
            }

        # Inject execution output into code context for panel.
        panel_code = code if not sandbox_context else f"{code}\n\n{sandbox_context}"

        # 2. Panel (parallel, resilient). Reviewer skills from the manifest
        # extend every reviewer's system prompt.
        reviewer_system_extra = ""
        if reviewer_role and (reviewer_role.skills or reviewer_role.rag_ns):
            rctx = await build_context(spec=panel_code, role=reviewer_role)
            reviewer_system_extra = rctx.system
        reviewer_fallback = reviewer_role.fallback or None if reviewer_role else None
        t_panel = time.perf_counter()
        reviews: list = []
        tasks = [
            asyncio.create_task(
                agents.review_code(
                    m,
                    panel_code,
                    system_extra=reviewer_system_extra,
                    fallback=reviewer_fallback,
                )
            )
            for m in members
        ]
        for fut in asyncio.as_completed(tasks):
            review = await fut
            reviews.append(review)
            member = next(m for m in members if m["name"] == review.reviewer)
            yield {
                "type": "review",
                "review": review.model_dump(),
                "usage": summarize_usage(usages).model_dump(),
                "context": _context_event(agents.review_context_stats(member, panel_code)),
            }
        rlog(
            "info",
            "panel done (%.1fs): %d/%d answered",
            time.perf_counter() - t_panel,
            sum(1 for r in reviews if r.ok),
            len(reviews),
        )

        # 3. Consensus
        consensus: ConsensusReport = await agents.build_consensus(
            reviews, fallback=consensus_role.fallback or None if consensus_role else None
        )
        yield {
            "type": "consensus",
            "consensus": consensus.model_dump(),
            "usage": summarize_usage(usages).model_dump(),
            "context": _context_event(agents.consensus_context_stats(reviews)),
        }

        # 4. Lead verdict
        verdict = await agents.lead_verdict(
            spec,
            code,
            consensus.model_dump_json(),
            fallback=lead_role.fallback or None if lead_role else None,
        )
        files = verdict["files"] or coded["files"]

        summary = summarize_usage(usages)
        rlog(
            "info",
            "run done (%.1fs): %d calls, %d/%d tokens, cost=%s",
            time.perf_counter() - t0,
            summary.calls,
            summary.input_tokens,
            summary.output_tokens,
            str(summary.cost) if summary.cost_known else "unknown",
        )

        exec_model: SandboxResult | None = exec_result

        yield {
            "type": "result",
            # members = the panel configs actually used (retry endpoints need
            # them to replay a reviewer against the same model).
            "members": members,
            "result": PipelineResult(
                spec=spec,
                code=code,
                language=coded["language"],
                reviews=reviews,
                consensus=consensus,
                verdict=verdict["verdict"],
                verdict_degraded=bool(verdict.get("degraded", False)),
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
            user = (
                f"Task: {spec}\n\nContext so far:\n{accumulated}"
                if accumulated
                else f"Task: {spec}"
            )
            # Declarative context: skills -> system prompt, rag_ns -> user.
            system_extra = ""
            if role.skills or role.rag_ns:
                ctx = await build_context(spec=user, role=role, rag_hits=rag_sources or None)
                system_extra = ctx.system
                user = ctx.user

            prov, mod = (
                providers.resolve_name(role.model) if "/" in role.model else quota.coder_model()
            )
            rt = _runtime_for_role(team, role)
            tok = llm.set_step(role_name)
            try:
                output = await agents.governed_call(
                    prov,
                    mod,
                    user,
                    system=system_extra or None,
                    max_tokens=role.max_tokens or config.CODER_MAX_TOKENS,
                    fallback=role.fallback or None,
                    tools=rt,
                )
            finally:
                llm.reset_step(tok)

            outputs[role_name] = output
            accumulated = f"{accumulated}\n\n[{role_name}]:\n{output}".strip()
            rlog("info", "step %s done (%.1fs)", role_name, time.perf_counter() - t0)
            yield {
                "type": "step",
                "role": role_name,
                "output": output,
                "usage": summarize_usage(usages).model_dump(),
                "context": _context_event(agents.context_stats("", user, prov, mod)),
            }

        summary = summarize_usage(usages)
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
                user = (
                    f"Iteration {i}. Task: {spec}\n\nContext so far:\n{accumulated}"
                    if accumulated
                    else f"Iteration {i}. Task: {spec}"
                )
                # Declarative context: skills -> system prompt, rag_ns -> user.
                system_extra = ""
                if role.skills or role.rag_ns:
                    ctx = await build_context(spec=user, role=role, rag_hits=rag_sources or None)
                    system_extra = ctx.system
                    user = ctx.user

                prov, mod = (
                    providers.resolve_name(role.model) if "/" in role.model else quota.coder_model()
                )
                rt = _runtime_for_role(team, role)
                tok = llm.set_step(f"{role_name}:{i}")
                try:
                    output = await agents.governed_call(
                        prov,
                        mod,
                        user,
                        system=system_extra or None,
                        max_tokens=role.max_tokens or config.CODER_MAX_TOKENS,
                        fallback=role.fallback or None,
                        tools=rt,
                    )
                finally:
                    llm.reset_step(tok)

                outputs[role_name].append(output)
                accumulated = f"{accumulated}\n\n[{role_name} i={i}]:\n{output}".strip()
                yield {
                    "type": "iteration",
                    "i": i,
                    "role": role_name,
                    "output": output,
                    "usage": summarize_usage(usages).model_dump(),
                    "context": _context_event(agents.context_stats("", user, prov, mod)),
                }
                if "[DONE]" in output:
                    done = True
                    break
            if done:
                break

        summary = summarize_usage(usages)
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
    "pipeline": run_pipeline,  # type: ignore[dict-item]
    "loop": run_loop,  # type: ignore[dict-item]
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
        raise ValueError(f"Unknown topology {team.topology!r}. Available: {list(_REGISTRY)}")
    return fn(team, spec, context=context, rag_sources=rag_sources, run_id=run_id)
