# Teams and topologies

A **team** is a YAML file under `teams/`. It declares which models play which
roles, which topology orchestrates them, and optional per-role settings
(sandbox, skills, tools, RAG namespace). No application code changes are needed
to add a new domain.

## Anatomy of a team YAML

```yaml
# teams/my-team.yaml

topology: consensus        # consensus | pipeline | loop (required)
sandbox: false             # default sandbox flag for the whole team

roles:
  coder:                   # role name (arbitrary, topology-specific)
    model: zen/deepseek-v3-0324   # "provider/model-id" (required)
    fallback: []           # list of fallback providers tried in order
    max_tokens: 8000       # LLM budget for this role
    skills: [coding]       # list of skill names (loads skills/<name>/SKILL.md)
    tools: []              # MCP tool names (requires mcp SDK)
    rag_ns: ""             # RAG namespace tag (informational for now)
    sandbox: false         # per-role override

  reviewer:                # panel role: may have 'members' instead of a single model
    model: zen/deepseek-v3-0324
    members:               # optional explicit panel list (overrides REVIEW_PANEL env)
      - name: reviewer-a
        provider: zen
        model: deepseek-v3-0324
      - name: reviewer-b
        provider: zen
        model: qwen3-coder
```

All fields except `model` are optional. Unset fields fall back to sane defaults
defined in `src/config.py`.

## The three topologies

### `consensus` (default)

```
coder -> [sandbox opt-in] -> panel (parallel) -> consensus -> lead
```

Designed for code review. The **coder** produces code, the **panel** reviews it
independently in parallel (one goroutine per reviewer), the **consensus** step
scores each issue by the fraction of reviewers who agreed, and the **lead**
arbitrates and produces final corrected code.

Key roles (by convention): `coder`, `reviewer`, `consensus`, `lead`.

Events emitted: `code`, `review` (one per panel member), `consensus`, `result`.

Built-in teams: `consensus.yaml` (pure LLM), `consensus-tested.yaml`
(sandbox-enabled).

### `pipeline` (sequential)

```
role_1 -> role_2 -> role_3  (each receives previous output as context)
```

Designed for multi-step workflows where each step builds on the last:
planner decides what to do, executor does it, verifier checks the result.
The role names are arbitrary (the topology iterates `roles` in YAML order).

Events emitted: `step` (one per role), `result`.

Built-in team: `sre.yaml` (planner -> executor -> verifier).

### `loop` (iterative)

```
roles cycle 1..max_iterations  (stops early on "[DONE]" in any output)
```

Designed for iterative tasks where completion time is unknown: reconnaissance,
exploitation, fuzzing. The cycle repeats until any role emits `[DONE]` in its
response or `max_iterations` is reached (default: 3).

Events emitted: `iteration` (one per role per cycle), `result`.

Built-in team: `pentest.yaml` (recon -> exploit -> reporter).

## Adding a domain (1 file, 0 code)

1. Copy the closest existing team as a template:
   ```
   cp teams/consensus.yaml teams/my-domain.yaml
   ```
2. Set `topology`, `sandbox`, and `roles`. Use `provider/model-id` for each
   model. Pick the topology that matches the workflow.
3. Optionally create `skills/my-domain/SKILL.md` with domain expertise that
   will be injected into the system prompt of roles that list it.
4. Reference the team by name:
   ```
   # CLI
   make run SPEC="..." TEAM=my-domain
   # API
   POST /api/run  {"spec": "...", "team": "my-domain"}
   ```

Run `make test` to verify the YAML loads cleanly (the team loader is tested in
`tests/test_teams_and_topologies.py`).

## Skills

Skills are Markdown files at `skills/<name>/SKILL.md`. When a role lists a
skill, the file is prepended to the system prompt. The content should be
concise, role-specific domain knowledge or instructions.

Built-in skills: `coding`, `review`, `sre`, `pentest`.

Example:
```yaml
roles:
  planner:
    skills: [sre, coding]
```

## Sandbox

When `sandbox: true` is set on a role, the coder's code is executed before
being reviewed. Reviewers see the execution output alongside the code.

Set `SANDBOX_ENGINE` to choose the engine:
- `docker` (default): throwaway container, `--network none`, read-only FS,
  memory + CPU limits. Requires Docker.
- `subprocess`: local subprocess with timeout. No isolation - dev only.
- `none`: skip execution entirely.
