# AGENTS.md — AI Coding Agent Instructions for personal-finance

> This file is the **single source of truth** for AI coding conventions in this project.
> It is read natively by Claude Code, Gemini CLI, GitHub Copilot, Cursor, Aider, and 28+
> other tools via the Linux Foundation's AGENTS.md standard (2026).
>
> Tool-specific files (CLAUDE.md, GEMINI.md, .github/copilot-instructions.md) extend this
> file with tool-specific configuration. Keep the core rules here.

---

## Project Overview

**Name:** personal-finance
**Package:** `personal_finance`
**Description:** Personal finance and budget management app, using data pipelining, machine learning, AI, and data visualization to make where money is going immediately intuitive.
**Repo:** https://github.com/mcintalmo/personal-finance
**Python:** >= 3.14
**Layout:** src layout (`src/personal_finance/`)

---

## Planning Docs & Task Discipline

- **[docs/ARCHITECTURE.md](./docs/ARCHITECTURE.md)** — system design, component choices, core schema. Read before structural changes.
- **[docs/FEATURES.md](./docs/FEATURES.md)** — full feature list, grouped by phase.
- **[docs/PLAN.md](./docs/PLAN.md)** — phase sequence and the working agreement for every phase.
- **[TODO.md](./TODO.md)** — the live task list. **Exactly one task is marked ⏳ IN PROGRESS at any time.** Mark your task in progress before starting; mark it done only when `/run-checks` (or the raw check sequence below) is green. Do not start work outside the current phase.

---

## Toolchain (non-negotiable)

| Tool | Purpose | Command |
|------|---------|---------|
| `uv` | Package & env manager | `uv sync`, `uv add <pkg>`, `uv run <cmd>` |
| `ruff` | Linting + formatting | `uv run ruff check .` / `uv run ruff format .` |
| `ty` | Type checking | `uv run ty check src/` |
| `pytest` | Testing | `uv run pytest` |
| `pre-commit` | Git hooks | `pre-commit install` (once) |

**Never** suggest pip, poetry, pdm, hatch, black, isort, flake8, mypy, or pyright as replacements. This project uses uv/ruff/ty exclusively.

---

## Project Skills

This repo ships three Claude Code skills in `.claude/skills/`. Invoke them with `/`:

| Skill | Invoke | What it does |
|-------|--------|-------------|
| `run-checks` | `/run-checks` | Full CI sequence: ruff format → ruff lint → ty check → pytest |
| `commit` | `/commit` | Pre-flight clean-up + Conventional Commits message + `git commit` |
| `new-feature` | `/new-feature` | Guided workflow: understand → plan → implement → test → verify |

**`/run-checks` should be called after every code change** before declaring work done. It mirrors exactly what CI runs.

**`/commit` has `disable-model-invocation: true`** — Claude will not commit autonomously. You invoke it explicitly.

---

## MCP Servers

Two MCP servers are configured in `.mcp.json` and available to all AI agents working in this repo:

| Server | Provides | Requires |
|--------|----------|---------|
| `github` | PR management, issues, code search, Actions runs, Dependabot alerts | `GITHUB_TOKEN` env var |
| `fetch` | Fetch any URL — docs, Stack Overflow, RFCs — as clean text | Nothing |

Set `GITHUB_TOKEN` in your shell or `.env.local`. A fine-grained PAT scoped to this repo is sufficient; no `repo:admin` scope needed for day-to-day use.

---

## Before Every Commit

Run this sequence and fix all errors before committing:

```bash
uv run ruff format .          # auto-format
uv run ruff check . --fix     # auto-fix lint issues
uv run ty check src/          # type check (no auto-fix)
uv run pytest                 # tests with coverage
```

If `pre-commit` is installed, `git commit` runs these automatically.

---

## Code Style Rules

### Python version and syntax
- Target **Python 3.14+** syntax throughout.
- Use built-in generic types: `list[str]`, `dict[str, int]`, `tuple[int, ...]` — **not** `List`, `Dict`, `Tuple` from `typing`.
- Use `X | Y` union syntax instead of `Union[X, Y]`.
- Use `X | None` instead of `Optional[X]`.
- Use `from __future__ import annotations` only if needed for forward references on Python < 3.10; it is **not** needed for 3.14+.
- Prefer `pathlib.Path` over `os.path` for all filesystem operations.

### Type hints
- **All** public functions, methods, and module-level variables must have type annotations.
- Private helpers (`_foo`) should also be annotated where not obvious.
- Use `TYPE_CHECKING` guard for annotations that would cause circular imports:
  ```python
  from __future__ import annotations
  from typing import TYPE_CHECKING
  if TYPE_CHECKING:
      from some_module import SomeType
  ```

### Data models
- Use **`dataclasses.dataclass`** for simple, immutable data containers with no validation.
- Use **`pydantic.BaseModel`** for any data that requires validation, serialization, or comes from external sources (API responses, user input, config files with complex types).
- Use **`pydantic_settings.BaseSettings`** for application configuration and settings. Never use plain dataclasses or dicts for settings.

### Error handling
- Raise specific exception types. Never `raise Exception("...")`.
- Define custom exceptions in `src/personal_finance/exceptions.py`.
- Always handle exceptions at the boundary layer (HTTP handlers, CLI entrypoints), not deep in business logic.

### Imports
- Standard library → third-party → first-party (`personal_finance`) — ruff enforces this.
- Use absolute imports from the package root: `from personal_finance.module import Thing`, not relative `..module`.

### Docstrings
- Public modules, classes, and functions should have Google-style docstrings.
- One-liners are fine for obvious functions.

---

## Project Structure

```
src/personal_finance/
├── __init__.py          # package version + public API re-exports
├── config.py            # pydantic_settings.BaseSettings subclass
├── exceptions.py        # custom exception hierarchy
└── ...                  # feature modules

tests/
├── __init__.py
├── conftest.py          # shared fixtures
└── test_*.py            # mirror the src/ structure

infra/
└── terraform/           # IaC (fill in for your cloud provider)
```

---

## Environments

The project supports three environments: **development**, **staging**, and **production**.

- Local dev config lives in `.env.local` (gitignored, never committed).
- `.env.example` is the canonical template — keep it updated when adding new env vars.
- CI/CD environments use GitHub Actions environment variables and secrets.
- Load settings via the `pydantic_settings.BaseSettings` class in `src/personal_finance/config.py`.

Never hardcode secrets, API keys, or environment-specific values in source code.

---

## Testing Conventions

- Test files live in `tests/` and mirror the `src/personal_finance/` structure.
- Use `pytest` fixtures (`conftest.py`) for shared setup.
- Aim for ≥ 80% coverage (enforced in CI).
- Unit tests should be fast and not require external services. Use `unittest.mock` or `pytest-mock` to mock I/O.
- Integration tests that need external services should be marked `@pytest.mark.integration` and skipped in standard CI runs.

---

## CI/CD

- All CI runs on **GitHub Actions**.
- CI runs on every push and PR to `main`: ruff → ty → pytest.
- CD deploys on merge to `main` after CI passes.
- See `.github/workflows/ci.yml` and `.github/workflows/cd.yml`.

---

## What NOT to Do

- Do not add files to `.gitignore` that are project-specific without updating `.env.example` accordingly.
- Do not use `print()` for logging in library code; use `logging` or a structured logger.
- Do not merge PRs with failing CI.
- Do not commit `.env.local` or any file containing secrets.
- Do not introduce new dependencies without updating `pyproject.toml` via `uv add`.
