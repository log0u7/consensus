## Description

<!-- What does this PR change and why? Link the issue: Fixes #123 -->

## How to test

<!-- Commands or steps to verify the change -->

## Checklist

- [ ] Title follows [Conventional Commits](https://www.conventionalcommits.org/) (`type(scope): description`)
- [ ] Branch named `feat/*`, `fix/*`, `docs/*`, `ci/*`, or `chore/*`
- [ ] `make check` passes locally (lint + typecheck + tests)
- [ ] Tests added for new pure logic
- [ ] Docs updated (README / CONTRIBUTING / docs/*) when behavior changes
- [ ] No secrets committed (`.env` stays gitignored)
- [ ] Sandbox invariants untouched (network none, read-only FS, memory/cpu caps, timeout, no secrets mounted)
