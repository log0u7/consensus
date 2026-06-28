# AGENTS.md

Guidance for AI agents working in this repository. Read this before making
changes. Read `CONTRIBUTING.md` for the Git workflow and commit convention.

## What this project is

Multi-agent code review on top of any OpenAI-compatible or Anthropic provider.
A **coder** writes code, a **panel** of independent models reviews it in
parallel, a **consensus** step scores each issue by how many reviewers agreed,
and a **lead** model arbitrates and produces final code. A FastAPI app exposes
a run pipeline and a chat with the lead, served as a single-page UI. A
Textual TUI is also available. All LLM calls go through `llm.py`; no SDK, no
agent framework.

## Tech stack

- Python 3.12, `async` throughout.
- `httpx` directly against providers (no SDK).
- FastAPI + uvicorn. Pydantic v2. pgvector + embeddings for optional RAG.
- `aiolimiter` (rate limiting) + `tenacity` (retry/backoff) in `governor.py`.
- Docker Compose + `make`. Single-page UI: plain HTML/JS, vendored highlight.js.

## Key files

- `src/config.py` - env loading, provider registry (`PROVIDERS`), panel
  parsing, per-role model/token settings, logging setup.
- `src/llm.py` - two transports (openai-compatible, anthropic Messages + SSE
  streaming); single `_client(provider)` factory; per-call usage capture via
  `usage_scope` / `Usage`; `parse_json` / `complete_json_obj` JSON recovery.
- `src/governor.py` - rate-limit (aiolimiter) + retry (tenacity) + provider
  fallback chain per call.
- `src/agents.py` - `write_code`, `review_code`, `build_consensus`,
  `lead_verdict`, `lead_chat` / `lead_chat_stream`.
- `src/pipeline.py` - orchestration: `run_streaming` (async generator, SSE
  events) and `run` (non-streaming wrapper).
- `src/sessions.py` - session store with TTL and LRU cap; memory or postgres.
- `src/quota.py` - low-quota toggle (degraded model/panel when throttled;
  Lead never downgraded).
- `src/rag.py` - pgvector (default) or sqlite-vec backend + embeddings.
- `src/archive.py` - pack file trees into zip/tar/tar.gz/tar.bz2/tar.xz/7z.
- `src/api.py` - FastAPI endpoints: `/api/run(/stream)`, `/api/chat(/stream)`,
  `/api/chat/regen-artifacts`, `/api/archive`, `/api/quota`, `/api/health`.
- `src/models.py` - Pydantic schemas, `Usage`, `CostSummary`, `Artifact`
  (with `sanitize_path`).
- `src/static/index.html` - single-page UI.

## Running and testing

The app runs inside Docker talking to a provider. For offline tests (no
provider needed):

```
make check    # lint + typecheck + test (ZEN_API_KEY=dummy is set by the Makefile)
make test     # tests only
```

For a real run (Zen key in .env):

```
make up
make run SPEC="write a Python class that does X"
```

## Conventions and invariants

- No LLM SDK, no agent framework: talk to providers with `httpx` in `llm.py`.
- Provider resolution goes through `config.get_provider(name)`. Never hardcode
  base URLs or auth headers outside `config.py` and `llm.py`.
- Prompts must request strict JSON; parse with `parse_json` / `complete_json_obj`
  which repair fenced/garbled output and retry.
- The panel is resilient: a failing reviewer returns `ok=False` and is skipped.
  The lead is also resilient: if its JSON cannot be parsed, `lead_verdict`
  returns a degraded verdict. A run always completes.
- The `consensus_score` is the core signal and must never trust the model
  blindly: `flagged_by` is validated against the real panel, deduped, and the
  score is derived in code (clamped to 1.0). Preserve this invariant.
- Use the `logging` module (configured in `config.py`). Never log secrets.
- Streaming endpoints keep a non-streaming fallback.
- Multi-file output: sanitize artifact paths (zip-slip) on ingestion and before
  archiving.
- SOLID/DRY/KISS/YAGNI: one responsibility per module, single `_client()`
  factory, no premature abstraction.

## Provider specifics

- Model IDs use the format `provider/model-id` in env vars (e.g.
  `zen/deepseek-r1-0528`, `anthropic/claude-opus-latest`).
- Zen uses `/chat/completions`. The `/responses` endpoint also works for some
  models; use `call_openai_compatible` for both.
- Reasoning models spend tokens before emitting JSON: keep the high
  `max_tokens` already set for reviewers and the lead.
- 429/503 are retried by the governor (tenacity + aiolimiter). When retries
  are exhausted and `AUTO_LOW_QUOTA=1`, low-quota mode is enabled automatically.

## Security

- Loopback-only, no application auth, CORS restricted to `ALLOWED_ORIGINS`.
- Input sizes capped before any billable call.
- Artifact paths sanitized against zip-slip in `models.sanitize_path` and
  again in `archive._validated`.
- Never commit secrets. Only `.env.example` (placeholders) is tracked.

## Do / don't

- Do branch from `main` for all changes; follow the commit convention.
- Do prefer editing existing files over creating new ones.
- Don't add LLM SDKs or agent frameworks (breaks the framework-free invariant).
- Don't commit secrets or `.env` files other than `.env.example`.
