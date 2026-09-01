<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="consensus.webp">
    <img src="consensus.webp" alt="Consensus" width="800">
  </picture>
</p>

[![CI](https://github.com/log0u7/consensus/actions/workflows/ci.yml/badge.svg)](https://github.com/log0u7/consensus/actions/workflows/ci.yml)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](https://opensource.org/licenses/Apache-2.0)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue.svg)](https://www.python.org/downloads/)
[![code style: ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)
[![Checked with mypy](https://img.shields.io/badge/checked%20with-mypy-blue.svg)](https://mypy-lang.org/)

# Consensus

Multi-agent orchestration running on any OpenAI-compatible or Anthropic provider.
Free by default on [Zen](https://opencode.ai) (no credit card required).

Domains are declared as **team manifests** (`teams/*.yaml`). The default team
performs multi-agent **code review**: a coder writes, a panel of independent
models reviews in parallel, a consensus step scores each issue by agreement,
and a lead arbitrates and produces the final code.

Other built-in teams: **SRE/DevOps** (planner -> executor -> verifier) and
**pentest/CTF** (recon -> exploit -> report loop). Adding a domain requires only
a new YAML file - no application code changes.

## Architecture

```mermaid
flowchart TD
    spec([spec + team.yaml]) --> pipeline

    subgraph pipeline["pipeline.py - dispatcher"]
        rag[RAG optional] --> topology
        topology{topology}
    end

    topology -->|consensus| cons_flow
    topology -->|pipeline| seq_flow
    topology -->|loop| loop_flow

    subgraph cons_flow["consensus team"]
        coder["CODER"] --> sandbox["sandbox\nopt-in"]
        sandbox --> panel["PANEL\nparallel"]
        panel --> consensus["CONSENSUS\nscores by agreement"]
        consensus --> lead["LEAD\nverdict + chat"]
    end

    subgraph seq_flow["SRE team"]
        planner --> executor --> verifier
    end

    subgraph loop_flow["pentest team"]
        recon --> exploit --> reporter
    end
```

All LLM calls: httpx -> provider, routed through `governor.py`
(rate-limit + retry + fallback). No SDK, no agent framework.

## Providers

| Name         | Transport          | Default base URL                          |
|--------------|--------------------|-------------------------------------------|
| `zen`        | OpenAI-compatible  | `https://opencode.ai/zen/v1` (free)       |
| `openrouter` | OpenAI-compatible  | `https://openrouter.ai/api/v1`            |
| `openai`     | OpenAI-compatible  | configurable `OPENAI_BASE_URL`            |
| `anthropic`  | Anthropic Messages | `https://api.anthropic.com/v1`            |
| `local`      | OpenAI-compatible  | `LOCAL_BASE_URL` (llama.cpp, Ollama, ...) |

Models use the `provider/model-id` format: `zen/big-pickle`,
`anthropic/claude-opus-latest`, `local/qwen3-8b`.

Costs and context windows are derived at runtime from OpenRouter's public
catalog (cached in `pricing.db`); local models cost 0. Prompt caching is
native: Anthropic gets `cache_control` on the system prefix, llama.cpp gets
`cache_prompt`, and cached tokens are surfaced in the UI (`cached input`).

## Quickstart

The fastest path is the setup wizard: it imports keys already saved by
opencode, probes each provider, suggests a routing and writes `.env`.

1. Configure:
   ```
   make setup
   ```
   or by hand:
   ```
   cp env.example .env
   # edit .env: one provider key is enough (e.g. ZEN_API_KEY, free)
   ```
2. Start:
   ```
   make up
   ```
   Open http://localhost:8800

### Fork in 5 minutes

```
git clone https://github.com/log0u7/consensus.git
cd consensus
make setup          # or: cp env.example .env and set one provider key
make up
```

That is it. The stack starts two containers: `consensus-app` (port 8800) and
`consensus-pgvector` (Postgres + pgvector, used by the optional RAG feature).

To run a task on the CLI instead of the UI:
```
make run SPEC="Write a Python function that validates an email address"
```

### Free demo mode (zero cost)

`make setup-auto` writes a complete, non-interactive **free-tier test
configuration**. All you need is one free OpenRouter key
(https://openrouter.ai). The wizard:

- imports keys already saved by opencode when present,
- routes every role to free models: `cohere/north-mini-code:free` as coder,
  `z-ai/glm-5.2:free` as consensus + lead, and a review panel of 3 distinct
  free models with a 32k output budget each (reasoning models think before
  answering),
- raises the 429 retry budget (free pools answer 429 in bursts),
- generates the compose `PG_PASSWORD` and seeds the pricing catalog
  (`pricing.db`) so costs show up from the first run.

Expected behaviour on the free tier - by design, never a crash:

- Runs take 1-3 minutes; 429 bursts are retried automatically.
- A reviewer whose pool is saturated may fail and is simply skipped
  (`ok=False`); consensus is computed over whoever answered.
- If the consensus aggregation or the Lead itself is unreachable after
  retries, the run **still completes**: a degraded consensus report and/or a
  degraded verdict (carrying the coder's code) is produced instead. A
  degraded verdict means the free pool was saturated upstream, not an app
  error.
- The Retry buttons act on live sessions: after an app/container restart
  they return 404 - re-run the task.
- To lift the limits: add credits to your OpenRouter account (raises the
  per-model `:free` daily cap), add a free Zen key with `make setup`, or
  switch to the paid routing below.

### Paid testing routing

`make setup-paid` rewrites the routing (keys and `PG_PASSWORD` are kept) to
normal OpenRouter models - one vendor per role, no free-pool 429 noise:

| Role      | Model                                       |
|-----------|---------------------------------------------|
| coder     | `openai/gpt-5.4-mini`                       |
| consensus | `google/gemini-3.5-flash-lite`              |
| lead      | `openai/gpt-5.3-codex`                      |
| panel     | `gemini-3.5-flash-lite`, `gpt-5.4-mini`, `deepseek-v4-flash-latest` |

Indicative cost per small test run: ~$0.03-0.10 (real costs show up in the
UI and CLI summary). Requires a positive OpenRouter credit balance - the
wizard validates each routing model and flags `402 Payment Required`
(add credits at openrouter.ai/settings/credits). `make setup-auto` switches
back to the free demo at any time.

To add a new domain without touching application code:
```
cp teams/consensus.yaml teams/my-domain.yaml
# edit my-domain.yaml: set topology, models, roles
# optionally add skills/my-domain/SKILL.md
```

## Teams and topologies

Teams are YAML files in `teams/`. Three topologies are available:

| Topology    | Flow                                  | Example team     |
|-------------|---------------------------------------|------------------|
| `consensus` | coder -> panel -> consensus -> lead   | `consensus.yaml` |
| `pipeline`  | role1 -> role2 -> role3 (sequential)  | `sre.yaml`       |
| `loop`      | roles cycle until `[DONE]`            | `pentest.yaml`   |

To use a non-default team via the CLI:
```
# (currently via pipeline.run directly; UI team selection coming in a future version)
```

### Adding a domain

1. Create `teams/<name>.yaml` with `topology`, `sandbox`, and `roles`.
2. Add `skills/<name>/SKILL.md` if domain expertise is needed.
3. Run `make test`.

No application code changes required.

## Sandbox (opt-in)

When `sandbox: true` is set on a role, the coder's generated code is executed
before the panel reviews it. Reviewers see actual execution output.

**Engines** (set `SANDBOX_ENGINE`):
- `docker` (default): throwaway container, `--network none`, read-only FS,
  memory + CPU limits. Requires Docker.
- `subprocess`: local subprocess with timeout. **No real isolation** - local
  dev only.
- `none`: skip execution.

## Skills

Skills are Markdown files loaded into the prompt when a role references them.
Bundled skills: `coding`, `review`, `sre`, `pentest`.

Add a skill: create `skills/<name>/SKILL.md`. Reference in team YAML:
```yaml
roles:
  planner:
    skills: [sre, coding]
```

## MCP tools (optional)

Tools are provided by MCP servers (Serena for LSP, custom infra tools, etc.).
The `mcp` SDK is a soft dependency - only needed when tools are listed in a team.

```
pip install mcp
```

**Serena (LSP)** - reduces token usage by providing symbolic code navigation
instead of dumping whole files. Launch externally:
```
uvx --from git+https://github.com/oraios/serena \
  serena start-mcp-server --context ide-assistant --project .
```
Then list it under `tools:` in your team manifest.

## Cache

Set `RESPONSE_CACHE=1` to cache identical LLM calls locally. Useful during
development and for repeated runs on the same code.

```
RESPONSE_CACHE=1
CACHE_BACKEND=sqlite      # or "memory" (default, lost on restart)
CACHE_DB_PATH=cache.db
```

## Make targets

| Target                  | Action                                            |
|-------------------------|---------------------------------------------------|
| `make setup`            | Interactive provider setup (writes `.env`)        |
| `make setup-auto`       | Tuned free-tier test `.env`, no questions asked   |
| `make setup-check`      | Probe configured providers, no changes            |
| `make up` / `start`     | Start the stack, detached (builds if needed)      |
| `make down` / `stop`    | Stop and remove the stack                         |
| `make update` / `reload`| Rebuild and recreate the app after code changes   |
| `make logs`             | Follow the app logs                               |
| `make run SPEC="..."`   | Run the pipeline on the CLI                       |
| `make index`            | Index `docs-projet/` into the RAG store           |
| `make check`            | Lint + typecheck + test (CI entrypoint)           |

`DEV=1` adds hot reload. `ENV=<name>` merges `.env.<name>` and layers
`docker-compose.<name>.yml`.

## RAG (optional, off by default)

1. Put documents under `docs-projet/` (.md, .txt, .py, .rst).
2. `make index`
3. Set `use_rag=true` per request (API or CLI).

Two backends: `pgvector` (default, bundled in compose) and `sqlite`
(`RAG_BACKEND=sqlite`, single file, no server).

## Rate limits and low-quota mode

The governor retries 429/503 with exponential backoff (honouring `Retry-After`).
When retries are exhausted, `AUTO_LOW_QUOTA=1` switches on low-quota mode:

- Coder and consensus drop to `LOW_QUOTA_MODEL`.
- The panel shrinks to `LOW_QUOTA_PANEL_SIZE` reviewers.
- **The lead is never downgraded.**

Per-role fallback: `CODER_FALLBACK=local` falls back to a local LLM when Zen
is down.

Toggle low-quota manually from the header pill or `POST /api/quota`.

## Configuration reference

All in `.env`. See `env.example` for the full reference with comments, or run
`make setup` to have it written for you.

| Variable            | Default                    | Purpose                             |
|---------------------|----------------------------|-------------------------------------|
| `ZEN_API_KEY`       | (one provider is required) | Zen provider key                    |
| `CODER_MODEL`       | `zen/big-pickle`           | Coder role model                    |
| `LEAD_MODEL`        | `zen/deepseek-v4-flash-free` | Lead role model                   |
| `CHAT_MODEL`        | (empty -> `LEAD_MODEL`)    | Chat/exploration model              |
| `REVIEW_PANEL`      | (Zen default panel)        | `name:provider/model[:max_tokens]`  |
| `SANDBOX_ENGINE`    | `docker`                   | `docker`, `subprocess`, or `none`   |
| `RESPONSE_CACHE`    | `0`                        | Set `1` to enable local cache       |
| `RAG_BACKEND`       | `pgvector`                 | `pgvector` or `sqlite`              |
| `AUTO_LOW_QUOTA`    | `1`                        | Auto low-quota on 429 exhaustion    |
| `CODER_FALLBACK`    | (empty)                    | Fallback provider(s) for coder      |

## Security

- Binds to loopback only; no application auth.
- CORS restricted to `ALLOWED_ORIGINS`.
- Input sizes capped before any billable call.
- Artifact paths sanitized against zip-slip (ingestion + archive).
- Sandbox: Docker `--network none`, read-only FS, resource caps, no secrets mounted.
- No secrets baked into the image; `.gitignore` excludes all `.env*` files.
  The tracked template is `env.example` (placeholder values only).

## File structure

```
consensus/
  README.md
  Makefile
  docker-compose.yml / docker-compose.dev.yml
  Dockerfile
  requirements.txt / requirements-dev.txt
  env.example      tracked env template (placeholders only)
  teams/          team manifests (YAML)
  skills/         skill files (SKILL.md per domain)
  docs-projet/    RAG documents (drop files here)
  src/
    config.py     env + provider registry
    providers.py  resolve() + capability metadata
    pricing.py    dynamic pricing + context windows (OpenRouter catalog)
    llm.py        httpx transports (openai-compatible + Anthropic + SSE)
    governor.py   rate-limit + retry + fallback
    agents.py     coder, reviewer, consensus, lead
    pipeline.py   dispatcher (RAG + topologies.run)
    roles.py      Role/Team + YAML loader
    topologies.py consensus / pipeline / loop
    sandbox.py    Docker / subprocess / no-op sandbox
    cache.py      local response cache
    setup.py      interactive provider setup wizard (`make setup`)
    context.py    AgentContext builder (stable prefix)
    skills.py     SKILL.md loader
    mcp_client.py MCP server manager
    rag.py        pgvector + sqlite-vec
    archive.py    zip/tar/7z packing
    models.py     Pydantic schemas
    sessions.py   session store
    quota.py      low-quota toggle
    api.py        FastAPI + SSE
    static/       SPA + vendored highlight.js (BSD-3)
  tui/            Textual terminal UI
  docs/           detailed documentation (teams, providers, RAG, UI)
  tests/          offline unit tests (179+ tests)
```

## Documentation

| Topic                             | File                        |
|-----------------------------------|-----------------------------|
| Teams, topologies, adding a domain | [docs/teams.md](docs/teams.md) |
| Providers, adding a provider      | [docs/providers.md](docs/providers.md) |
| RAG: indexing, backends, opt-in   | [docs/rag.md](docs/rag.md)   |
| Web UI, TUI, API endpoints        | [docs/ui.md](docs/ui.md)     |

## License

Apache 2.0, Copyright 2026 log0u7. See [LICENSE](LICENSE) and [NOTICE](NOTICE).

This project bundles [Highlight.js](https://highlightjs.org/) under the
BSD 3-Clause License (attribution in [NOTICE](NOTICE)).
