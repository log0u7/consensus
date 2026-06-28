# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

---

## [0.2.0] - 2026-06-29

### Added

- **Tests - quota** (`tests/test_quota.py`, 16 tests): low-quota toggle,
  coder/consensus downgrade, Lead-never-downgraded invariant, `profile()`
  helper.
- **Tests - agents** (`tests/test_agents.py`, 14 tests): `write_code`
  single/multi-file, zip-slip path sanitisation, `review_code` resilience
  (`ok=False` on failure), severity normalisation, `build_consensus`
  `flagged_by` validation/dedup/clamp, `lead_verdict` degraded fallback.
- **Tests - pipeline** (`tests/test_pipeline.py`, 15 tests): `run_streaming`
  event sequence and order, `run()` returns `PipelineResult`, `CostSummary`
  aggregation, RAG not called by default, `lead_system_for`.
- **Tests - topologies** (6 new): `run_pipeline` sequential steps and context
  accumulation; `run_loop` `max_iterations`, early-stop on `[DONE]`, output
  dict shape.
- **Tests - edge cases** (11 new): single-reviewer score=1.0, equal-score
  severity sort, panel only includes `ok` reviewers, `min_score=0` keeps
  all rows, score field in hits, Artifact roundtrip, JSON-serializability of
  sessions.
- **Documentation** (`docs/`): four new reference documents:
  - `docs/teams.md`: team YAML anatomy, three topologies, adding a domain,
    skills, sandbox.
  - `docs/providers.md`: model reference format, built-in providers, transports,
    adding a provider, governor retry/fallback, per-reviewer `max_tokens`.
  - `docs/rag.md`: why off by default, pgvector vs sqlite-vec, quickstart, how
    it works, config reference, CLI tools.
  - `docs/ui.md`: web UI, TUI key bindings/screens, full API endpoint table,
    SSE event types, session lifecycle, run request schema.
- **README**: "Fork in 5 minutes" quickstart section; Documentation table
  linking to `docs/`.

### Tests

179 tests passing (up from 120 at end of v0.2.0 development). New test files:
`test_quota.py`, `test_agents.py`, `test_pipeline.py`. Extended:
`test_teams_and_topologies.py`, `test_consensus_scoring.py`,
`test_rag_threshold.py`, `test_session_serialization.py`.

---

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

120 tests passing (up from 68 at v0.1.0). New: governor, provider registry,
teams/topologies (parity), sandbox (subprocess + Docker), cache (memory +
sqlite), skills, context builder, MCP client, SRE/pentest team loading.

---

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

[Unreleased]: https://github.com/log0u7/consensus/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/log0u7/consensus/compare/v0.1.0...v0.2.0
[0.2.0-dev]: https://github.com/log0u7/consensus/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/log0u7/consensus/releases/tag/v0.1.0
