# Security posture

Scope: personal, loopback-bound deployment. The controls below describe what
the application enforces today and the residual risks accepted by design.

## Network exposure

- The API binds to loopback only (`127.0.0.1:8800` in docker-compose; the
  uvicorn host is operator-controlled).
- No application-level authentication: any local process can call the API.
  State-changing endpoints (`/api/run`, `/api/chat*`, `/api/quota`,
  `/api/archive`, `DELETE /api/session/*`) are reachable without credentials.
- CORS is restricted to `ALLOWED_ORIGINS` (default
  `http://localhost:8800`). Do not expose the port beyond loopback.

## LLM-generated code execution (sandbox)

- Sandbox execution is opt-in per team role (`sandbox: true`) and per engine
  (`SANDBOX_ENGINE=docker|subprocess|none`, default `docker`).
- DockerSandbox enforces: no network, read-only rootfs, dropped
  capabilities, no-new-privileges, PID/memory/CPU caps, unprivileged user,
  workspace mounted read-only, no secrets mounted.
- SubprocessSandbox is resource-limited (ulimits) but provides NO
  isolation: it can read host files, open network sockets and see inherited
  environment. Never select it for untrusted code.
- When a requested execution is skipped (e.g. docker missing), a warning is
  logged into the run events: generated code was NOT executed.

## Prompt-injection surface

- RAG chunks and skill files are third-party text. They are wrapped in
  `<untrusted>` markers in the prompt so models treat them as data, but
  delimiting is mitigation-in-depth, not a guarantee. Treat knowledge-base
  contents as attacker-influenceable input.

## MCP tool servers

- Team manifests spawn local processes for stdio transports by design;
  every spawned command is logged at INFO level.
- HTTP transports require https for remote hosts (plain http is limited to
  loopback). There is no built-in allowlist; review manifest URLs before
  adding servers.

## Secrets

- Providers read API keys from environment variables (see `.env.example`);
  nothing is hardcoded. `.env*` files are gitignored.
- Secret scanning: gitleaks pre-commit hook + CI job over full history.
- Exceptions returned to clients carry the exception type only; details
  stay in server logs (they may embed internal base URLs).

## Data persistence

- Response cache (`RESPONSE_CACHE=1`): full prompts and responses are
  stored on disk (memory backend by default; sqlite writes `cache.db`
  unencrypted).
- Sessions: chat history and run results persist in memory (TTL/LRU) or in
  PostgreSQL (`SESSION_BACKEND=postgres`), unencrypted.
- Delete sessions you do not want retained (`DELETE /api/session/{id}`).

## Supply chain

- Dependencies are exact-pinned (`requirements*.txt`).
- CI runs `pip-audit --strict` (direct + transitive) and gitleaks on every
  push/PR. Bump pins deliberately and re-run checks after each bump.

## Reporting

This is a personal project without a security disclosure channel. Do not
deploy it on shared or internet-exposed infrastructure.
