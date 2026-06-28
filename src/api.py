"""FastAPI backend: runs the consensus pipeline and exposes a chat with the
Lead. Serves the single-page UI from src/static.

Sessions are in-memory with a TTL and an LRU cap (see sessions.py), so a
long-running process does not leak memory. They are lost on restart unless
SESSION_BACKEND=postgres is set.

Security posture: the app binds to loopback only (see docker-compose) and
has no application-level auth. CORS is restricted to ALLOWED_ORIGINS.
"""

import asyncio
import json
from collections.abc import AsyncIterator

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import archive, config, llm, pipeline, quota
from .models import Artifact, CostSummary, PipelineResult
from .sessions import store

config.setup_logging()

app = FastAPI(title="Consensus")

# Keep SSE connections alive through proxies/SSH tunnels during long silent
# phases (a single Opus call can take ~75s with no bytes on the wire).
HEARTBEAT_SECONDS = float(config.SSE_HEARTBEAT_SECONDS)
# Headers that disable proxy buffering so events flush immediately.
SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


async def with_heartbeat(gen: AsyncIterator[str]) -> AsyncIterator[str]:
    """Wrap an SSE string generator, emitting ': ping' comments while the inner
    generator is idle so the connection is not dropped mid-run. Comment lines
    (starting with ':') are ignored by the client's SSE reader.

    The inner generator runs as one background task feeding a queue, so it keeps
    a single contextvars Context (the pipeline relies on usage_scope, which must
    enter and exit in the same context). The heartbeat is a timeout on the queue.
    """
    queue: asyncio.Queue = asyncio.Queue()
    _DONE = object()

    async def pump():
        try:
            async for chunk in gen:
                await queue.put(("data", chunk))
        except Exception as exc:  # noqa: BLE001 - surface as an SSE error event
            await queue.put(("error", f"{type(exc).__name__}: {exc}"))
        finally:
            await queue.put(("done", _DONE))

    task = asyncio.ensure_future(pump())
    try:
        while True:
            try:
                kind, payload = await asyncio.wait_for(queue.get(), HEARTBEAT_SECONDS)
            except TimeoutError:
                yield ": ping\n\n"
                continue
            if kind == "done":
                break
            if kind == "error":
                yield f"data: {json.dumps({'type': 'error', 'error': payload})}\n\n"
                break
            yield payload
    finally:
        if not task.done():
            task.cancel()


app.add_middleware(
    CORSMiddleware,
    allow_origins=config.ALLOWED_ORIGINS,
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["*"],
)


class RunRequest(BaseModel):
    spec: str = Field(min_length=1, max_length=config.MAX_SPEC_CHARS)
    use_rag: bool = False


class RunResponse(BaseModel):
    session_id: str
    result: PipelineResult


class ChatRequest(BaseModel):
    session_id: str
    message: str = Field(min_length=1, max_length=config.MAX_MESSAGE_CHARS)


class ChatResponse(BaseModel):
    reply: str
    usage: CostSummary = CostSummary()


class RegenRequest(BaseModel):
    session_id: str


class RegenResponse(BaseModel):
    files: list[Artifact] = []
    usage: CostSummary = CostSummary()


class ArchiveRequest(BaseModel):
    files: list[Artifact] = Field(min_length=1)
    format: str = "zip"
    root: str = "project"


# Limit concurrent runs (seamless: extra runs wait their turn). The semaphore
# is created lazily so it binds to the running event loop.
_run_semaphore: asyncio.Semaphore | None = None


def _run_slot() -> asyncio.Semaphore:
    global _run_semaphore
    if _run_semaphore is None:
        _run_semaphore = asyncio.Semaphore(config.MAX_CONCURRENT_RUNS)
    return _run_semaphore


def _seed_session(result: PipelineResult) -> str:
    """Create a session holding the result and a Lead history seeded with its
    own verdict, so the follow-up chat continues coherently."""
    system = pipeline.lead_system_for(result)
    history = [
        {"role": "user", "content": "Give your verdict on this code."},
        {
            "role": "assistant",
            "content": f"Verdict: {result.verdict}\n\n{result.rationale}",
        },
    ]
    return store.create({"result": result, "system": system, "history": history})


@app.post("/api/run", response_model=RunResponse)
async def api_run(req: RunRequest):
    if not req.spec.strip():
        raise HTTPException(status_code=400, detail="empty spec")
    async with _run_slot():
        result: PipelineResult = await pipeline.run(req.spec, use_rag=req.use_rag)
    sid = _seed_session(result)
    return RunResponse(session_id=sid, result=result)


@app.post("/api/run/stream")
async def api_run_stream(req: RunRequest):
    """SSE variant of /api/run. Emits the pipeline events as they happen:
      {"type":"code",...} {"type":"review",...} {"type":"consensus",...}
    then a final {"type":"result", "session_id":..., "result":...}. On the
    result event a session is created so the client can chat afterwards.
    """
    if not req.spec.strip():
        raise HTTPException(status_code=400, detail="empty spec")

    async def gen():
        try:
            # Acquire a run slot inside the generator: if all slots are busy the
            # client waits here, kept alive by the SSE heartbeat.
            async with _run_slot():
                async for event in pipeline.run_streaming(req.spec, use_rag=req.use_rag):
                    if event["type"] == "result":
                        result = PipelineResult.model_validate(event["result"])
                        event["session_id"] = _seed_session(result)
                    yield f"data: {json.dumps(event)}\n\n"
        except Exception as exc:  # noqa: BLE001
            yield f"data: {json.dumps({'type': 'error', 'error': f'{type(exc).__name__}: {exc}'})}\n\n"

    return StreamingResponse(
        with_heartbeat(gen()), media_type="text/event-stream", headers=SSE_HEADERS
    )


@app.post("/api/chat", response_model=ChatResponse)
async def api_chat(req: ChatRequest):
    sess = store.get(req.session_id)
    if sess is None:
        raise HTTPException(status_code=404, detail="session expired or unknown")
    sess["history"].append({"role": "user", "content": req.message})
    from . import agents

    tok = llm.set_step("chat")
    try:
        with llm.usage_scope() as usages:
            reply = await agents.lead_chat(sess["system"], sess["history"])
    finally:
        llm.reset_step(tok)
    sess["history"].append({"role": "assistant", "content": reply})
    store.save(req.session_id, sess)
    return ChatResponse(reply=reply, usage=pipeline.summarize_usage(usages))


@app.post("/api/chat/stream")
async def api_chat_stream(req: ChatRequest):
    """SSE variant of /api/chat. Emits `data:` lines:
      {"delta": "..."}             text chunks as they arrive
      {"done": true, "usage": {}}  final event with the turn's usage
      {"error": "..."}             on failure
    The full reply is appended to the session history once the stream ends.
    """
    sess = store.get(req.session_id)
    if sess is None:
        raise HTTPException(status_code=404, detail="session expired or unknown")
    sess["history"].append({"role": "user", "content": req.message})
    from . import agents

    async def gen():
        parts: list[str] = []
        tok = llm.set_step("chat")
        try:
            with llm.usage_scope() as usages:
                async for delta in agents.lead_chat_stream(sess["system"], sess["history"]):
                    parts.append(delta)
                    yield f"data: {json.dumps({'delta': delta})}\n\n"
            reply = "".join(parts)
            sess["history"].append({"role": "assistant", "content": reply})
            store.save(req.session_id, sess)
            usage = pipeline.summarize_usage(usages).model_dump()
            yield f"data: {json.dumps({'done': True, 'usage': usage})}\n\n"
        except Exception as exc:  # noqa: BLE001
            # Roll back the unanswered user turn so a retry is coherent.
            if sess["history"] and sess["history"][-1]["role"] == "user":
                sess["history"].pop()
            yield f"data: {json.dumps({'error': f'{type(exc).__name__}: {exc}'})}\n\n"
        finally:
            llm.reset_step(tok)

    return StreamingResponse(
        with_heartbeat(gen()), media_type="text/event-stream", headers=SSE_HEADERS
    )


@app.post("/api/chat/regen-artifacts", response_model=RegenResponse)
async def api_regen_artifacts(req: RegenRequest):
    """Ask the Lead to emit the current project as a file tree, so the chat can
    produce downloadable artifacts on demand (e.g. after 'split into a role')."""
    sess = store.get(req.session_id)
    if sess is None:
        raise HTTPException(status_code=404, detail="session expired or unknown")
    from . import agents

    with llm.usage_scope() as usages:
        files = await agents.lead_regen_artifacts(sess["system"], sess["history"])
    if files:
        sess["result"].files = files
        store.save(req.session_id, sess)
    return RegenResponse(files=files, usage=pipeline.summarize_usage(usages))


@app.post("/api/archive")
async def api_archive(req: ArchiveRequest):
    """Pack a file tree into the requested format and stream it back. The LLM
    never compresses; this builds the archive server-side (stdlib + py7zr)."""
    try:
        data = archive.build_archive(req.files, req.format, root=req.root or "project")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    media_type, ext = archive.FORMATS[req.format]
    filename = f"{req.root or 'project'}.{ext}"
    return Response(
        content=data,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/api/archive/formats")
async def api_archive_formats():
    return {"formats": list(archive.FORMATS.keys())}


@app.delete("/api/session/{session_id}")
async def api_delete_session(session_id: str):
    store.delete(session_id)
    return Response(status_code=204)


class QuotaRequest(BaseModel):
    low_quota: bool


@app.get("/api/quota")
async def api_quota_get():
    return quota.profile()


@app.post("/api/quota")
async def api_quota_set(req: QuotaRequest):
    quota.set_low_quota(req.low_quota)
    return quota.profile()


@app.get("/api/health")
async def health(check_provider: str | None = None):
    out = {"status": "ok", "sessions": len(store), "panel_size": len(quota.panel())}
    if check_provider:
        out["provider"] = await llm.provider_reachable(check_provider)
    return out


# Silence noisy browser probes (favicon, Chrome devtools) with empty 204s.
@app.get("/favicon.ico")
async def favicon():
    return Response(status_code=204)


@app.get("/.well-known/appspecific/com.chrome.devtools.json")
async def chrome_devtools_probe():
    return Response(status_code=204)


# UI: serve index.html at / and static assets.
app.mount("/", StaticFiles(directory="src/static", html=True), name="static")
