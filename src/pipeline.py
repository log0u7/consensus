"""Pipeline orchestration: thin dispatcher over team topologies.

The default team is "consensus" (coder -> panel -> consensus -> lead).
Pass team_name to run_streaming() / run() to use a different team.

RAG is handled here (before dispatching) so topologies stay RAG-agnostic.

The API, UI, and TUI are unaffected: the "consensus" team emits the same
event shapes (code / review / consensus / result) as before.
"""

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator

from . import agents, config
from .models import CostSummary, PipelineResult, Usage

log = logging.getLogger(__name__)

_DEFAULT_TEAM = "consensus"


def summarize_usage(usages: list[Usage]) -> CostSummary:
    cost = sum(u.cost for u in usages if u.cost is not None)
    cost_known = any(u.cost is not None for u in usages)
    return CostSummary(
        calls=len(usages),
        input_tokens=sum(u.input_tokens for u in usages),
        output_tokens=sum(u.output_tokens for u in usages),
        cost=round(cost, 6),
        cost_known=cost_known,
    )


async def run_streaming(
    spec: str,
    use_rag: bool = False,
    run_id: str | None = None,
    team_name: str = _DEFAULT_TEAM,
) -> AsyncIterator[dict]:
    """Stream pipeline events for the given team.

    Default team "consensus" emits:
      {"type": "code",      "code", "language", "usage"}
      {"type": "review",    "review": ..., "usage"}
      {"type": "consensus", "consensus": ..., "usage"}
      {"type": "result",    "result": <PipelineResult dict>}

    Other topologies emit topology-specific events plus a final "result".
    """
    from . import roles as roles_mod
    from . import topologies

    rid = run_id or uuid.uuid4().hex[:8]

    # RAG (optional, off by default) - resolved before dispatching.
    context = ""
    rag_sources: list[dict] = []
    if use_rag:
        try:
            from . import rag
            hits = await rag.search(spec, k=config.RAG_TOP_K)
            context = "\n\n".join(f"[{h['source']}]\n{h['content']}" for h in hits)
            rag_sources = [
                {"source": h["source"], "chunk_idx": h["chunk_idx"], "score": round(h["score"], 3)}
                for h in hits
            ]
            log.info("[run %s] RAG injected %d chunk(s)", rid, len(hits))
        except Exception as exc:  # noqa: BLE001
            log.warning("[run %s] RAG disabled (%s: %s)", rid, type(exc).__name__, exc)

    team = roles_mod.load(team_name)
    topo = await topologies.run(team, spec, context=context, rag_sources=rag_sources, run_id=rid)
    async for event in topo:
        yield event


async def run(
    spec: str,
    use_rag: bool = False,
    team_name: str = _DEFAULT_TEAM,
) -> PipelineResult:
    """Non-streaming wrapper: returns the final PipelineResult."""
    final: PipelineResult | None = None
    async for event in run_streaming(spec, use_rag=use_rag, team_name=team_name):
        if event["type"] == "result":
            raw = event["result"]
            # Consensus topology returns a full PipelineResult dict.
            # Other topologies return a simpler dict; wrap it gracefully.
            if "consensus" in raw:
                final = PipelineResult.model_validate(raw)
            else:
                # Non-consensus topologies: return a minimal PipelineResult.
                from .models import ConsensusReport
                final = PipelineResult(
                    spec=spec,
                    code=raw.get("outputs", {}).get("coder", ""),
                    consensus=ConsensusReport(),
                    cost_summary=CostSummary.model_validate(raw.get("cost_summary", {})),
                )
    assert final is not None, "pipeline did not emit a result event"
    return final


def lead_system_for(result: PipelineResult) -> str:
    """Build the Lead system prompt grounded in this run's code and consensus."""
    return agents.LEAD_SYSTEM_TEMPLATE.format(
        spec=result.spec,
        code=result.final_code or result.code,
        consensus=result.consensus.model_dump_json(),
    )


if __name__ == "__main__":
    import sys
    config.setup_logging()
    spec = " ".join(sys.argv[1:]) or (
        "Write a Python script that connects to PostgreSQL and lists users inactive for 90 days."
    )
    res = asyncio.run(run(spec))
    print("\n===== PANEL =====")
    for rv in res.reviews:
        status = "ok" if rv.ok else f"FAILED ({rv.error})"
        print(f"  {rv.reviewer}: {status}, {len(rv.issues)} issues")
    print("\n===== CONSENSUS =====")
    for it in res.consensus.issues:
        bar = f"{int(it.consensus_score * 100):3d}%"
        print(f"  [{bar} {it.severity:8s}] {it.title}  <- {', '.join(it.flagged_by)}")
    print(f"\nsummary: {res.consensus.summary}")
    print(f"\n===== VERDICT: {res.verdict} =====")
    print(res.rationale)
    cs = res.cost_summary
    cost = f"{cs.cost}" if cs.cost_known else "unknown"
    print(
        f"\n===== USAGE: {cs.calls} calls, {cs.input_tokens} in / "
        f"{cs.output_tokens} out tokens, cost={cost} ====="
    )
    print("\n===== FINAL CODE =====")
    print(res.final_code)
