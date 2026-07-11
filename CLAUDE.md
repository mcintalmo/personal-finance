# CLAUDE.md — Claude Code Instructions for personal-finance

> **Core project rules live in [AGENTS.md](./AGENTS.md)** — read that first.
> This file contains Claude Code–specific configuration that extends AGENTS.md.

---

## Quick Reference

```bash
# Install deps
uv sync

# Run the full check sequence (use this — it matches CI exactly)
/run-checks

# Implement a new feature end-to-end
/new-feature

# Stage + clean + commit with a Conventional Commits message
/commit
```

Raw commands if needed:
```bash
uv run ruff format . && uv run ruff check . --fix   # format + lint
uv run ty check src/                                 # type check
uv run pytest                                        # tests + coverage
```

---

## Memory and Context

- This project uses **src layout**: the importable package is `src/personal_finance/`, not `personal_finance/` at the root.
- Always run `uv run <command>` rather than bare `python` or direct tool invocations, to ensure the uv-managed virtual environment is used.
- The virtual environment is at `.venv/` (created by `uv sync`).
- Settings are loaded from `.env.local` in development (see `.env.example`).

---

## Claude-Specific Behaviour

### When asked to add a feature
1. Understand where it fits in the project structure (see AGENTS.md → Project Structure).
2. Write or update the implementation in `src/personal_finance/`.
3. Write tests in `tests/` mirroring the module path.
4. Run the full check sequence above before declaring done.
5. If a new dependency is required, add it with `uv add <package>` (or `uv add --group dev <package>` for dev-only).

### When asked to fix a bug
1. Reproduce it with a failing test first, if feasible.
2. Fix the implementation.
3. Confirm the test passes and no other tests regress.

### When asked to refactor
1. Keep the public API stable unless explicitly told to break it.
2. Run the full check sequence.
3. Check that coverage hasn't dropped below 80%.

### Tool use
- Prefer running shell commands with `uv run` to stay inside the managed environment.
- When reading files, prefer reading the specific file rather than `cat`-ing the whole directory.
- Use `uv run ruff check --select <RULE> <file>` for targeted lint checks during debugging.

---

## Files Claude Should Never Modify Without Being Asked

- `.github/workflows/` — CI/CD configuration (propose changes, don't auto-edit)
- `pyproject.toml` — dependency changes should be confirmed before adding
- `.env.local` — never read or write secrets files
- `infra/terraform/` — IaC requires human review

---

## Commit Message Convention

Use [Conventional Commits](https://www.conventionalcommits.org/):

```
feat: add user authentication module
fix: handle None return from database query
chore: upgrade ruff to 0.16
docs: update README with setup instructions
refactor: extract config loading into BaseSettings
test: add integration tests for API client
```
