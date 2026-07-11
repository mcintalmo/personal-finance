---
description: >
  Implement a new feature end-to-end: understand the requirement, plan the approach,
  implement in src/personal_finance/, write matching tests, and run checks. Use when
  asked to add a feature, implement something, or build a new capability.
allowed-tools: Bash(uv run *) Bash(uv add *) Bash(git status*) Bash(git diff *)
---

Implement a new feature following personal-finance's conventions. Work through each
phase in order; do not skip ahead to writing code before planning.

## Phase 1: Understand

Before writing any code:

- If the requirement is ambiguous, ask one clarifying question. Do not assume.
- Read the relevant existing files in `src/personal_finance/` to understand patterns
  already in use (error handling style, how config is accessed, exception types, etc.)
- Check `tests/conftest.py` for available fixtures.

## Phase 2: Plan

State concisely (3–6 bullet points):
- Which file(s) in `src/personal_finance/` will be created or modified
- The corresponding test file(s) in `tests/`
- Any new runtime dependency (needs `uv add <package>`)
- Any new dev dependency (needs `uv add --group dev <package>`)

Wait for confirmation before proceeding if the plan involves significant new dependencies
or changes to the public API.

## Phase 3: Implement

**File placement:**
- Source code: `src/personal_finance/` — never at the repo root
- New modules: one file per concern; avoid god-modules
- Public exports: add to `src/personal_finance/__init__.py` if they are part of the
  public API

**Code style (enforced by ruff and ty):**
- Type hints on all public functions and methods — use Python 3.10+ syntax:
  `list[str]`, `dict[str, int]`, `X | None` (not `Optional[X]`)
- Data models:
  - Simple containers → `@dataclass`
  - Validated/external data → `pydantic.BaseModel`
  - Application config → `pydantic_settings.BaseSettings`
- Access settings via `from personal_finance.config import get_settings`
- Custom exceptions: add to `src/personal_finance/exceptions.py`, raise specific types
- Logging: use `logging.getLogger(__name__)`, never `print()` in library code

## Phase 4: Test

- Test file path mirrors the source path:
  `src/personal_finance/auth.py` → `tests/test_auth.py`
- Write at least one test per public function or method
- Use `pytest.mark.parametrize` for multiple input cases rather than repeated assertions
- Mock external I/O (`httpx`, DB calls, filesystem) — tests must not require live services
- Integration tests that need external services: mark with `@pytest.mark.integration`

## Phase 5: Verify

Run `/run-checks` to execute the full check sequence (ruff → ty → pytest).

If checks fail:
1. Fix the issue — do not suppress linter warnings with `# noqa` without explanation
2. Re-run `/run-checks` until everything is green

## Phase 6: Summarise

Report:
- What was implemented and in which files
- What tests were added and what they cover
- Coverage line from the pytest output (goal: ≥ 80%)
- Any follow-up work that was out of scope for this feature
