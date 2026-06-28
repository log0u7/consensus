"""Full consensus pipeline orchestration.

Flow:
  1. Coder writes code.
  2. Panel reviews in PARALLEL (resilient: failing reviewers are skipped).
  3. Consensus aggregator scores each issue by reviewer agreement.
  4. Lead arbitrates: verdict + corrected final code.

RAG is optional and off by default.
"""

import asyncio
import logging
import time
import uuid
from collections.abc import AsyncIterator

from . import agents, config, llm, quota
from .models import ConsensusReport, CostSummary, PipelineResult, Usage

log = logging.getLogger(__name__)


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
    spec: str, use_rag: bool = False, run_id: str | None = None
) -> AsyncIterator[dict]:
    """Run the pipeline as a stream of events, one per stage:

      {"type": "code",      "code", "language"}
      {"type": "review",    "review": <Review dict>}
      {"type": "consensus", "consensus": <ConsensusReport dict>}
      {"type": "result",    "result": <PipelineResult dict>}

    The terminal result event carries the full PipelineResult so a client can
    render the complete state without reassembling partial events.
    """
    rid = run_id or uuid.uuid4().hex[:8]

    def rlog(level, msg, *args):
        getattr(log, level)("[run %s] " + msg, rid, *args)

    with llm.usage_scope() as usages:
        t0 = time.perf_counter()

        # 1. Optional retrieval
        context = ""
        rag_sources: list[dict] = []
        if use_rag:
            try:
                from . import rag
                hits = await rag.search(spec, k=config.RAG_TOP_K)
                context = "\n\n".join(
                    f"[{h['source']}]\n{h['content']}" for h in hits
                )
                rag_sources = [
                    {
                        "source": h["source"],
                        "chunk_idx": h["chunk_idx"],
                        "score": round(h["score"], 3),
                    }
                    for h in hits
                ]
                rlog("info", "RAG injected %d chunk(s)", len(hits))
            except Exception as exc:  # noqa: BLE001
                context = ""
                rlog("warning", "RAG disabled (%s: %s)", type(exc).__name__, exc)

        # 2. Code
        coded = await agents.write_code(spec, context)
        code = coded["code"]
        rlog("info", "coder done (%.1fs)", time.perf_counter() - t0)
        yield {
            "type": "code",
            "code": code,
            "language": coded["language"],
            "usage": summarize_usage(usages).model_dump(),
        }

        # 3. Panel: emit each review as it arrives (not after the slowest one)
        t_panel = time.perf_counter()
        reviews: list = []
        tasks = [
            asyncio.create_task(agents.review_code(member, code))
            for member in quota.panel()
        ]
        for fut in asyncio.as_completed(tasks):
            review = await fut
            reviews.append(review)
            yield {
                "type": "review",
                "review": review.model_dump(),
                "usage": summarize_usage(usages).model_dump(),
            }
        rlog(
            "info",
            "panel done (%.1fs): %d/%d reviewers answered",
            time.perf_counter() - t_panel,
            sum(1 for r in reviews if r.ok),
            len(reviews),
        )

        # 4. Consensus
        consensus: ConsensusReport = await agents.build_consensus(reviews)
        yield {
            "type": "consensus",
            "consensus": consensus.model_dump(),
            "usage": summarize_usage(usages).model_dump(),
        }

        # 5. Lead verdict
        verdict = await agents.lead_verdict(spec, code, consensus.model_dump_json())
        files = verdict["files"] or coded["files"]

        summary = summarize_usage(usages)
        rlog(
            "info",
            "run done (%.1fs): %d calls, %d in / %d out tokens, cost=%s",
            time.perf_counter() - t0,
            summary.calls,
            summary.input_tokens,
            summary.output_tokens,
            f"{summary.cost}" if summary.cost_known else "unknown",
        )

        result = PipelineResult(
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
            usages=list(usages),
            cost_summary=summary,
        )
        yield {"type": "result", "result": result.model_dump()}


async def run(spec: str, use_rag: bool = False) -> PipelineResult:
    """Non-streaming wrapper: consumes run_streaming and returns the final result."""
    final: PipelineResult | None = None
    async for event in run_streaming(spec, use_rag=use_rag):
        if event["type"] == "result":
            final = PipelineResult.model_validate(event["result"])
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
