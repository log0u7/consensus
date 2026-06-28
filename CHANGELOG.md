# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [0.1.0] - 2026-06-28

Initial open-source release, derived from the mlaas-consensus project and
ported to run freely on Zen and any OpenAI-compatible or Anthropic provider.

### Added

- **Core pipeline** - multi-agent code review: coder writes code, a
  configurable panel reviews in parallel, consensus scores each issue by
  reviewer agreement, lead arbitrates the final result.
- **Multi-provider** - four provider types: `zen` (free, default),
  `openai` (generic OpenAI-compatible, configurable base_url), `anthropic`
  (native Messages API with SSE streaming), `local` (Ollama/vLLM/llama.cpp).
  Models referenced as `provider/model-id` in env vars.
- **Governor** - `src/governor.py`: per-provider rate-limit via `aiolimiter`,
  retry with exponential backoff + jitter via `tenacity`, provider fallback
  chain (e.g. zen -> local when monthly quota is exhausted).
- **Streaming** - pipeline and lead chat stream over SSE with heartbeat
  keepalive; non-streaming fallback for CLI and tests.
- **Resilience** - failing reviewers are skipped; lead JSON parse failures
  return a degraded verdict; runs always complete.
- **Consensus scoring** - `flagged_by` validated against real panel, deduped,
  score derived in code, clamped to 1.0. Never trusts model output blindly.
- **Low-quota mode** - process-wide toggle downgrades coder and consensus
  (lead never downgraded); auto-enables on 429 exhaustion.
- **Session management** - TTL + LRU cap; memory (default) or postgres backend.
- **Multi-file artifacts** - coder and lead emit `files: [{path, content}]`;
  zip-slip protection; server-side archive in zip, tar.*, and 7z.
- **RAG** - pgvector backend (default) plus optional sqlite-vec; off by default.
- **TUI** - Textual terminal interface with run, chat, artifact browser, and
  quota profile screens.
- **Web UI** - single-page interface with streaming chat, syntax highlighting,
  file explorer, usage pills, verdict pill, quota pill.
- **Tooling** - ruff, mypy, pytest with asyncio support; `make check` CI
  entrypoint; GitHub Actions CI.

### Design principles

SOLID, DRY, KISS, YAGNI. No LLM SDK, no agent framework; plain httpx. Single
`_client(provider)` factory. Minimal dependencies.

[0.1.0]: https://github.com/your-org/consensus/releases/tag/v0.1.0
