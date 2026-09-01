# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Changed

- License switched from MIT to Apache 2.0; bundled Highlight.js attribution
  moved to a `NOTICE` file.

## [0.4.0] - 2026-08-28

### Added

- **Provider-native context caching + usage accounting**: Anthropic system
  prompts are sent as `cache_control: ephemeral` blocks (non-streaming and
  streaming); `cached_tokens` is captured from
  `prompt_tokens_details.cached_tokens` (OpenAI-compatible) and
  `cache_read_input_tokens` (Anthropic, both call shapes), stored on
  `Usage.cached_tokens` and rolled up per provider in
  `CostSummary.by_provider`.
- **Dynamic pricing** (`src/pricing.py`): model prices and context windows are
  derived from OpenRouter's public catalog (no key needed), cached in
  `pricing.db` (`PRICING_TTL_HOURS=24`); background refresh never blocks a
  call (`PRICING_REFRESH=0` disables it). Unknown cost falls back to the
  catalog; local models cost 0.
- **Context meter**: every pipeline event now carries `context`
  (`system_tokens`, `user_tokens`, `total_tokens`, `model`,
  `context_window`, `est_input_cost`); the web UI shows a live `ctx` pill
  (prompt size vs model window) and the TUI logs context lines.
- **OpenRouter provider**: first-class `OPENROUTER_API_KEY` provider sending
  `"usage": {"include": true}` so real per-call costs are reported end to end.
- **Chat model routing**: `CHAT_MODEL` env (empty -> `LEAD_MODEL`) lets the
  exploration chat run on a different profile (e.g. a local model); shown in
  the quota pill and `/api/quota`.
- **Setup wizard** (`make setup`, `python -m src.setup`): imports API keys
  saved by opencode (auth.json), prompts for missing ones, probes providers
  (`/models` + 1-token completion for key validity), suggests a model routing
  from the models actually exposed (prefers free models), and writes `.env`
  (backup in `.env.bak`). `make setup-check` / `--check` validates the current
  environment non-interactively. `make setup-auto` / `--auto` writes a tuned
  free-tier test config (big-pickle coder, deepseek-v4-flash-free
  consensus/lead, 3-reviewer free panel, generated PG password) with zero
  prompts.
- **Generalized defaults**: `CODER_MODEL` defaults to `zen/big-pickle`,
  `CONSENSUS_MODEL`/`LEAD_MODEL` to `zen/deepseek-v4-flash-free`; the default
  panel no longer pins qwen/deepseek paid models; `/api/health?check_provider=all`
  probes every configured provider.
- **Paid testing routing**: `make setup-paid` (`--auto --paid`) rewrites the
  routing to normal OpenRouter models (`gpt-5.4-mini` coder,
  `gemini-3.5-flash-lite` consensus, `gpt-5.3-codex` lead, 3-vendor panel)
  keeping keys and `PG_PASSWORD`; `make setup-auto` switches back to the
  free demo. Keys/credentials never printed.
- **`env.example`**: provider-neutral tracked template (replaces
  `.env.example`); documented providers/routing/pricing sections.
- **Free demo mode**: `make setup-auto` writes a complete zero-cost test
  config from a single free OpenRouter key (free models for every role,
  3-reviewer panel with 32k budgets, raised 429 retry budget, generated
  `PG_PASSWORD`, seeded pricing catalog); documented limits and degraded
  behaviour in the README. The busiest free pool is only used for the Lead
  (coder/consensus/panel run on other free models) to avoid stacking 429s,
  and the auto-retry budget defaults to 3 attempts with a growing window
  (`RATE_LIMIT_MAX_RETRIES`, 0 disables).
- **Per-agent retry/refresh** (web UI + API): `POST /api/run/retry/reviewer`
  re-runs one panel reviewer against the same code/model then replays the
  consensus AND the Lead verdict with the updated panel;
  `POST /api/run/retry/lead` re-runs the verdict alone. Both are session
  based, SSE streamed, and accumulate usage/cost. The UI shows a Retry
  button on every reviewer card and a "Retry lead" button in the verdict
  popover (usable as a refresh, not just for failures), plus a "Retry run"
  button after a stream error (simple retry when the coder failed). Sessions
  now carry the panel members actually used.
- **Progress bar** (web UI, Task card): live pipeline progress
  (Coder -> Panel n/N -> Consensus -> Lead -> 100%) driven by the existing
  SSE events, red on stream error.
- **Diff tab** (web UI): GitHub-style Draft -> Final line diff (added/removed
  lines with line numbers), dependency-free; falls back gracefully above
  1000 lines or when the Lead made no changes.

### Changed

- `summarize_usage` is now the single usage aggregator (moved to
  `models.py`, re-exported by `pipeline.py`) and reports `cached_tokens`
  plus per-provider rollups.
- Test suite grown to 290 tests covering pricing, setup wizard, retry
  endpoints, degraded paths and session members.
- README, docs/providers.md and CONTRIBUTING.md updated for the new setup
  flow and pricing/caching behaviour.

### Fixed

- **A run always completes on provider failure**: a consensus aggregation
  failing after exhausted 429 retries (or any provider error) now degrades
  to a report with no issues (the Lead arbitrates on the raw reviews)
  instead of killing the stream; a failing Lead call degrades to a verdict
  carrying the coder's code, so the UI's Final tab and the CLI report are
  never empty. Restores the documented "run always completes" invariant at
  the consensus/lead steps.
- **UI Final tab**: tabs reordered to `Draft (Coder) | Final (Lead)`; files
  with empty content are ignored so a Lead that lists paths without content
  no longer blanks the Final pane (falls back to the code blob).
- **Docker image missing `teams/` and `skills/`**: any team-based run inside
  the container failed with `FileNotFoundError` on the manifest. Both
  directories are now copied into the image.
- **OpenAI-compatible `content: null`**: reasoning models can exhaust the
  token budget on thinking and answer `content: null`, which crashed JSON
  parsing (`None.strip()`); it now degrades to `""` so the JSON retry loop
  handles it. Reviewer budget is configurable per panel member
  (`name:provider/model:max_tokens`).
- **Provider error bodies**: non-retryable 4xx responses now log the first
  300 chars of the provider error body (previously invisible after
  `raise_for_status`).

## [0.3.0] - 2026-08-24

### Added

- **Free default config**: `.env.example.free` with zero-key Ollama local (or remote qwen9b), OpenRouter `:free` and Groq fallbacks; `_parse_panel` fixed for Ollama model IDs with colons; `make dev` target for bare-metal uvicorn.
- **Panel parser fix**: `_parse_panel` now robustly parses Ollama model IDs (`local/qwen:7b`) by treating trailing `:digits` as `max_tokens` only when >=256, otherwise as part of the model name.
- **Makefile dev target**: `make dev` launches uvicorn bare-metal on port 8800 with hot-reload, `.env` loaded, no Docker required.

## [0.2.0-dev] - 2026-06-28

### Added

- **Governor** (`src/governor.py`): per-provider rate-limit (aiolimiter),
  retry with exponential backoff + jitter (tenacity), provider fallback chain
  per role (`CODER_FALLBACK`, `REVIEWER_FALLBACK`, `CONSENSUS_FALLBACK`,
  `LEAD_FALLBACK`). All agent calls now route through the governor.
- **Provider registry** (`src/providers.py`): `resolve("provider/model-id")`
  returning a typed Provider object, capability metadata (reasoning,
  context_window, max_tokens) for known models, `has_reasoning()` helper.
- **Modular teams** (`src/roles.py`, `src/topologies.py`): `Role`/`Team`
  dataclasses with YAML loader (`teams/*.yaml`). Three pluggable topologies:
  `consensus` (original flow), `pipeline` (sequential), `loop` (iterative).
  Adding a domain requires only a new YAML file, no code change.
- **Team manifests**: `teams/consensus.yaml` (default), `teams/consensus-tested.yaml`
  (sandbox opt-in example), `teams/sre.yaml` (DevOps pipeline), `teams/pentest.yaml`
  (CTF loop with sandbox).
- **Sandbox** (`src/sandbox.py`): pluggable execution interface with
  `DockerSandbox` (safe default: `--network none`, read-only FS, mem/CPU
  limits), `SubprocessSandbox` (local dev only, no isolation), `NoSandbox`.
  Opt-in per role (`sandbox: true`). Execution output injected into panel
  context. New `SandboxResult` schema in `models.py`, `execution` field in
  `PipelineResult`. New `"execution"` SSE event type.
- **Cache** (`src/cache.py`): local response cache (memory LRU or sqlite)
  keyed by (model, messages) hash. Opt-in via `RESPONSE_CACHE=1`. Integrated
  transparently into `llm.py` transports. Prompt prefix stability documented
  and enforced in `context.py`.
- **Context builder** (`src/context.py`): `AgentContext` with stable prefix
  order (system -> skills -> tools -> RAG -> spec) to maximise provider
  prefix-cache hits.
- **Skills** (`src/skills.py`): load `skills/*/SKILL.md` on demand per role.
  Bundled skills: `coding`, `review`, `sre`, `pentest`.
- **Reference documentation** (`docs/`): four new documents - `docs/teams.md`
  (team YAML anatomy, topologies, adding a domain), `docs/providers.md`
  (model reference format, transports, governor retry/fallback, per-reviewer
  `max_tokens`), `docs/rag.md` (backends, quickstart, config reference),
  `docs/ui.md` (web UI, TUI screens, API endpoint table, SSE events).
- **README**: "Fork in 5 minutes" quickstart section; Documentation table.
- **MCP client** (`src/mcp_client.py`): async `MCPClientManager` connecting N
  MCP servers (stdio + Streamable HTTP), aggregating `list_tools`/`call_tool`.
  Soft dependency on `mcp` SDK (only required when tools are listed in a team).
- **CI** (`.github/workflows/ci.yml`): three independent jobs (lint, typecheck,
  test) with Python 3.12 + 3.13 matrix, `concurrency` to cancel stale runs,
  `permissions: contents: read`, pip cache on `~/.cache/pip`.

### Changed

- `pipeline.py` is now a thin dispatcher: loads team manifest, resolves RAG,
  delegates to `topologies.run()`. API/UI/TUI event shapes unchanged.
- `quota.py` uses `providers.resolve_name()` for model resolution (DRY).

### Tests

179 tests passing (up from 68 at v0.1.0). New test files: `test_quota.py`,
`test_agents.py`, `test_pipeline.py`. Extended: `test_teams_and_topologies.py`,
`test_consensus_scoring.py`, `test_rag_threshold.py`,
`test_session_serialization.py`. Covering low-quota invariants, zip-slip
sanitisation, consensus `flagged_by` validation/dedup/clamp, streaming event
order, pipeline/loop topologies, sandbox, cache, skills, context builder and
MCP client.

---

## [0.1.0] - 2026-06-28

Initial open-source release. Multi-agent code review running freely on Zen
and any OpenAI-compatible or Anthropic provider.

### Added

- **Core pipeline** - coder writes code, parallel panel reviews, consensus
  scores by agreement, lead arbitrates.
- **Multi-provider** - `zen` (free, default), `openai` (generic, configurable
  `base_url`), `anthropic` (native Messages API + SSE), `local` (Ollama/vLLM).
- **Streaming** - pipeline and lead chat over SSE with heartbeat keepalive.
- **Resilience** - failing reviewers skipped; lead JSON failure returns a
  degraded verdict; runs always complete.
- **Session management** - TTL + LRU cap; memory or postgres backend.
- **Multi-file artifacts** - zip-slip protection, server-side archives.
- **RAG** - pgvector (default) + sqlite-vec optional; off by default.
- **TUI** - Textual terminal interface.
- **Web UI** - single-page chat, syntax highlighting, quota pill.
- **MIT licence**.

[Unreleased]: https://github.com/log0u7/consensus/compare/v0.4.0...HEAD
[0.4.0]: https://github.com/log0u7/consensus/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/log0u7/consensus/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/log0u7/consensus/compare/v0.1.0...v0.2.0
[0.2.0-dev]: https://github.com/log0u7/consensus/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/log0u7/consensus/releases/tag/v0.1.0
