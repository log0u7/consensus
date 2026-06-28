# Contributing

Git workflow and conventions for this project.

## Branch model

- **`main`** - integration branch. Always buildable. Every feature or fix
  lands here through a short-lived work branch.
- **Work branches** - short-lived, created from an up-to-date `main`:
  - `feat/*` - a new feature.
  - `fix/*` - a bug fix or hardening change.
  - `docs/*` - documentation only.

## Workflow

```
git checkout main
git pull
git checkout -b feat/my-feature

# ... commit atomically ...

git checkout main
git merge --no-ff feat/my-feature
git branch -d feat/my-feature
```

Rules:
- Branch from `main`, one feature or fix per branch.
- Keep branches small; merge fast.

## Commit messages

[Conventional Commits](https://www.conventionalcommits.org/).

```
type(scope): short imperative description
```

Types: `feat`, `fix`, `docs`, `refactor`, `perf`, `test`, `build`, `ci`, `chore`.

Examples:
```
feat(pipeline): stream run stages over SSE
fix(llm): harden JSON parsing against fenced code
docs(readme): add Zen quickstart
```

## Secrets

- Never commit secrets. `.gitignore` excludes all `.env*` files except
  `.env.example`.
- `.env.example` holds only placeholders.

## Tests, lint, CI

All driven by the Makefile:

- `make lint` - ruff
- `make format` - ruff format + autofix
- `make typecheck` - mypy
- `make test` - pytest (offline, no provider key needed)
- `make check` - lint + typecheck + test (the CI entrypoint)

Add tests for new pure logic; `make check` must pass before merging.
