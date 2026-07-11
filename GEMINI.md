# GEMINI.md — Gemini CLI Instructions for personal-finance

> **Core project rules live in [AGENTS.md](./AGENTS.md)** — read that first.
> This file contains Gemini CLI–specific notes that extend AGENTS.md.

---

## Quick Reference

```bash
uv sync                                         # install deps
uv run ruff check . --fix && uv run ruff format .  # lint + format
uv run ty check src/                            # type check
uv run pytest                                   # tests with coverage
```

---

## Project Context

- **Package name:** `personal_finance` (importable as `import personal_finance`)
- **Layout:** src layout — `src/personal_finance/` is the Python package root
- **Python version:** 3.14+
- **Repo:** https://github.com/mcintalmo/personal-finance

---

## Key Conventions (Gemini reminders)

- Use `uv run` for all tool invocations — never bare `python`, `pytest`, `ruff`, or `ty` directly.
- Type hints are mandatory for all public APIs. Use Python 3.14+ syntax (`list[str]`, `X | None`).
- Data models: `dataclass` for simple structs, `pydantic.BaseModel` for validated data, `pydantic_settings.BaseSettings` for application config.
- Settings come from `.env.local` locally; GitHub Secrets in CI. Never hardcode values.
- All changes must pass `ruff check`, `ruff format --check`, `ty check`, and `pytest` before being considered complete.

---

## Workflow for Code Changes

1. Understand the change scope — check AGENTS.md for the relevant conventions.
2. Implement in `src/personal_finance/`.
3. Write or update tests in `tests/`.
4. Run the full check sequence above.
5. Summarise what changed and why.

---

See [AGENTS.md](./AGENTS.md) for the complete reference on code style, data models, environments, testing, and CI/CD.
