# AGENTS.md

Guidance for AI agents working in this repository. Read this before making
changes. Read `CONTRIBUTING.md` for the Git workflow and commit convention.

## What this project is

Multi-agent code review on top of any OpenAI-compatible or Anthropic provider.
The noyau orchestrates two interfaces (a provider = LLM endpoint, a tool =
MCP server) plus data (team manifests). Domains are declared in `teams/*.yaml`
without touching application code.

Default flow (team "consensus"):
**coder** writes code -> **panel** reviews in parallel -> **consensus** scores
by agreement -> **lead** arbitrates + produces final code -> chat with lead.

A FastAPI app exposes SSE streaming, a single-page UI, and a Textual TUI.
All LLM calls go through `llm.py`; no SDK, no agent framework.

## Tech stack

- Python 3.12+, `async` throughout.
- `httpx` directly against providers (no SDK).
- FastAPI + uvicorn. Pydantic v2.
- `aiolimiter` + `tenacity` in `governor.py` (rate-limit + retry + fallback).
- `pyyaml` for team manifests.
- pgvector + sqlite-vec for optional RAG.
- Docker for the sandbox (opt-in per team).
- `mcp` SDK (soft dep) for MCP tool integration.

## Key files

- `src/config.py` - env loading, `PROVIDERS` dict, panel parsing, model refs.
- `src/providers.py` - `resolve("provider/model")` -> (Provider, model), caps.
- `src/llm.py` - two transports (openai-compatible, Anthropic Messages + SSE);
  single `_client(provider)` factory; usage capture; response cache integration.
- `src/governor.py` - `aiolimiter` RPM + `tenacity` retry + provider fallback.
- `src/agents.py` - coder, reviewer, consensus, lead; all routed through governor.
- `src/roles.py` - `Role`/`Team` dataclasses + YAML loader from `teams/`.
- `src/topologies.py` - `consensus`, `pipeline`, `loop` orchestrators; dispatch.
- `src/pipeline.py` - thin dispatcher: RAG pre-fetch + `topologies.run()`.
- `src/sandbox.py` - `Sandbox` interface, `DockerSandbox`, `SubprocessSandbox`,
  `NoSandbox`; opt-in via `role.sandbox=true`.
- `src/cache.py` - local response cache (memory/sqlite); opt-in `RESPONSE_CACHE=1`.
- `src/context.py` - `AgentContext` builder (stable prefix order for cache hits).
- `src/skills.py` - load `skills/*/SKILL.md` on demand.
- `src/mcp_client.py` - `MCPClientManager` (stdio + Streamable HTTP).
- `src/rag.py` - pgvector (default) or sqlite-vec RAG backend.
- `src/archive.py` - zip/tar/7z packing with zip-slip protection.
- `src/api.py` - FastAPI endpoints + SSE + static UI.
- `src/models.py` - Pydantic schemas: `Usage`, `PipelineResult`, `SandboxResult`.
- `src/sessions.py` - TTL + LRU session store (memory or postgres).
- `src/quota.py` - low-quota mode (Lead never downgraded).

## Running and testing

The app runs inside Docker. For offline tests (no provider key needed):

```
make check    # lint + typecheck + test (ZEN_API_KEY=dummy set by Makefile)
make test     # tests only
```

For a real run (Zen key in .env):
```
make up
make run SPEC="write a Python class that does X"
```

## Conventions and invariants

- **No SDK, no agent framework**: all provider calls through `llm.py`/`governor.py`.
- **Provider resolution**: always through `providers.resolve()` or `config.get_provider()`.
  Never hardcode base URLs or auth headers outside `config.py`/`llm.py`.
- **JSON from LLMs**: request strict JSON in prompts; parse with `parse_json` /
  `complete_json_obj` which repair and retry. Never `json.loads` raw LLM output directly.
- **Panel resilience**: a failing reviewer returns `ok=False` and is skipped.
  A run always completes; the lead returns a degraded verdict on JSON parse failure.
- **Consensus score**: validated against the real panel, deduped, derived in code,
  clamped to 1.0. Never trust the model's `flagged_by` blindly. Preserve this invariant.
- **Governor**: every agent call goes through `governor.call()` (rate-limit + retry +
  fallback). Never bypass it with a direct `llm.complete()` in agents.
- **Context builder prefix order**: system -> skills -> tools -> RAG -> spec (volatile
  last). Maintain this order in `context.py` for cache efficiency.
- **Adding a domain**: create `teams/<name>.yaml` + optional `skills/<name>/SKILL.md`.
  Zero application code changes required.
- **Logging**: use the `logging` module. Never log secrets or API keys.
- **Multi-file artifacts**: sanitize paths (zip-slip) on ingestion and before archiving.

## Sandbox safety invariants (NEVER relax)

When `sandbox: true` is set in a team manifest, LLM-generated code is executed.
This is a real attack surface. The DockerSandbox enforces:

- `--network none` - no outbound network.
- `--read-only` - root FS read-only.
- `--memory` cap - no memory exhaustion.
- `--cpu-quota` cap - no CPU starvation.
- Timeout - stuck containers are killed.
- **No secrets mounted into the container.**

SubprocessSandbox has NO real isolation. It must never be used with untrusted
LLM-generated code in a shared or network-accessible environment.

## Provider specifics

- Model refs: `provider/model-id` format (e.g. `zen/deepseek-r1-0528`).
- Zen: OpenAI-compatible `/chat/completions`; real cost in `usage.cost_details.upstream_inference_cost`.
- Anthropic: native Messages API with SSE streaming; cost not reported (tokens only).
- OpenRouter via `openai` provider: set `OPENAI_BASE_URL=https://openrouter.ai/api/v1`.
- Reasoning models spend tokens before emitting JSON: keep high `max_tokens`.
- 429/503 retried by governor; `AUTO_LOW_QUOTA=1` auto-enables low-quota on exhaustion.

## Security

- Loopback-only bind, no application auth, CORS restricted to `ALLOWED_ORIGINS`.
- Input sizes capped before any billable call.
- Zip-slip protection in `models.sanitize_path` and `archive._validated`.
- Never commit secrets; only `.env.example` is tracked.

## Do / don't

- Do branch from `main`; follow Conventional Commits.
- Do prefer editing existing files over creating new ones.
- Do run `make check` before committing.
- Don't add LLM SDKs or agent frameworks.
- Don't bypass the governor in agents.
- Don't commit secrets or `.env` files other than `.env.example`.
- Don't relax DockerSandbox security invariants.
