# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased] - 0.2.0

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

[Unreleased]: https://github.com/log0u7/consensus/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/log0u7/consensus/releases/tag/v0.1.0
