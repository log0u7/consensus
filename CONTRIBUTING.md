# Contributing

Git workflow, conventions, and project structure for contributors.

## Branch model

- **`main`** - integration branch. Always buildable and tested. Every change
  lands here through a short-lived work branch.
- **Work branches** - created from an up-to-date `main`:
  - `feat/*` - a new feature.
  - `fix/*` - a bug fix or hardening change.
  - `docs/*` - documentation only.
  - `ci/*` - CI/tooling changes.

## Workflow

```
git checkout main && git pull
git checkout -b feat/my-feature

# ... commit atomically ...

git checkout main
git merge --no-ff feat/my-feature
git branch -d feat/my-feature
git push origin main
```

Rules:
- Branch from `main`, one feature or fix per branch.
- Keep branches small; merge fast.
- `make check` must pass before merging.

## Commit messages

[Conventional Commits](https://www.conventionalcommits.org/).

```
type(scope): short imperative description
```

Types: `feat`, `fix`, `docs`, `refactor`, `perf`, `test`, `build`, `ci`, `chore`.

Examples:
```
feat(sandbox): add DockerSandbox with network isolation
fix(llm): harden JSON parsing against fenced code
docs(readme): add SRE team quickstart
ci(github): add Python 3.13 to test matrix
```

## Secrets

- Never commit secrets. `.gitignore` excludes all `.env*` files except
  `.env.example`.
- `.env.example` contains only placeholders.

## Tests, lint, CI

All driven by the Makefile:

```
make lint       # ruff
make format     # ruff format + autofix
make typecheck  # mypy
make test       # pytest (offline, no provider key needed)
make check      # lint + typecheck + test (CI entrypoint)
```

Add tests for new pure logic. `make check` must be green before merging.

CI runs three independent jobs (lint, typecheck, test) on Python 3.12 and 3.13.

## Project structure

```
src/
  config.py      env loading, provider registry (PROVIDERS), panel parsing
  providers.py   resolve("provider/model") -> Provider + caps metadata
  llm.py         httpx transports (openai-compatible + Anthropic Messages + SSE)
  governor.py    rate-limit (aiolimiter) + retry (tenacity) + fallback chain
  agents.py      coder, reviewer, consensus, lead - all go through governor
  pipeline.py    thin dispatcher: loads team, delegates to topologies.run()
  roles.py       Role/Team dataclasses, teams/*.yaml loader
  topologies.py  consensus / pipeline / loop orchestrators
  sandbox.py     Sandbox interface, DockerSandbox, SubprocessSandbox, NoSandbox
  cache.py       local response cache (memory/sqlite), opt-in RESPONSE_CACHE=1
  context.py     AgentContext builder (stable prefix order for cache efficiency)
  skills.py      load skills/*/SKILL.md on demand
  mcp_client.py  MCPClientManager (stdio + Streamable HTTP), aggregates tools
  rag.py         pgvector (default) + sqlite-vec optional RAG
  models.py      Pydantic schemas: Usage, PipelineResult, SandboxResult, ...
  sessions.py    session store (TTL + LRU; memory or postgres backend)
  archive.py     pack file trees into zip/tar/tar.gz/tar.bz2/tar.xz/7z
  quota.py       low-quota mode toggle (Lead never downgraded)
  api.py         FastAPI endpoints + SSE streaming + static UI
teams/           team manifests (YAML)
skills/          skill files (SKILL.md per domain)
tui/             Textual terminal UI
tests/           offline unit tests
```

## Adding a team / domain

A team is pure data: create `teams/<name>.yaml` and optionally add skill files.
The application code does not change.

1. Create `teams/myteam.yaml`:
   ```yaml
   topology: consensus   # or pipeline / loop
   sandbox: false
   roles:
     coder:
       model: zen/deepseek-v3-0324
       skills: [coding]
       ...
   ```
2. Add skill files if needed: `skills/<name>/SKILL.md`.
3. Run `make test` to verify YAML loads and topology dispatches correctly.

See `teams/sre.yaml` (pipeline) and `teams/pentest.yaml` (loop) as examples.

## Dependencies

Runtime:
- `httpx` - HTTP transport (no LLM SDK by design).
- `pydantic` + `pydantic-settings` - schemas and validation.
- `fastapi` + `uvicorn` - API and SSE.
- `aiolimiter` + `tenacity` - rate-limit and retry in `governor.py`.
- `json-repair` - JSON recovery fallback in `llm.py`.
- `pyyaml` - team manifest loading.
- `psycopg[binary]` + `pgvector` - Postgres RAG backend (optional at runtime).
- `py7zr` - 7z archive support.
- `mcp` *(soft)* - MCP client SDK; only required when `tools:` are listed in a
  team manifest. Install with `pip install mcp`.

External tools (not Python packages):
- **Serena** - LSP server exposed as an MCP server. Run externally:
  ```
  uvx --from git+https://github.com/oraios/serena \
    serena start-mcp-server --context ide-assistant --project .
  ```
  Then reference it in a team YAML under `tools:` and configure it as an MCP
  stdio server. No Python dependency; Serena is never imported.

- **Docker** - required for `DockerSandbox` (the default when `sandbox: true`).
  Not needed if `SANDBOX_ENGINE=subprocess` or `SANDBOX_ENGINE=none`.

## Sandbox safety

When a Role has `sandbox: true`, the generated code is executed before the
panel reviews it. This is a significant attack surface.

**Invariants that must never be relaxed for DockerSandbox:**
- `--network none` - no outbound network from the container.
- `--read-only` - root filesystem is read-only.
- `--memory` cap - prevents memory exhaustion.
- `--cpu-quota` cap - prevents CPU starvation.
- No secrets mounted into the container (check `docker-compose.yml` and your
  `teams/*.yaml` for accidental volume mounts).
- Timeout enforced: a stuck container is killed.

**SubprocessSandbox provides NO real isolation.** It exists only for local
development on trusted code. Never use it with untrusted LLM-generated code
in a shared or exposed environment. The warning is logged at every invocation.

Select the engine via `SANDBOX_ENGINE=docker|subprocess|none` (default: `docker`).
