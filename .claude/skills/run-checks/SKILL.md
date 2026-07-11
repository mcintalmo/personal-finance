---
description: >
  Run the full CI check sequence for personal-finance: format with ruff, lint with ruff,
  type-check with ty, and test with pytest. Use after any code change, before committing,
  or whenever asked to verify the code is clean, passing, or ready to merge.
allowed-tools: Bash(uv run *)
---

Run the complete check sequence below **in order**. Stop immediately and report the full
output if any step fails — do not continue to the next step.

## Steps

1. **Format** (auto-fixes in place)
   ```
   uv run ruff format .
   ```

2. **Lint** (auto-fixes where possible)
   ```
   uv run ruff check . --fix
   ```

3. **Type check** (no auto-fix — errors must be resolved manually)
   ```
   uv run ty check src/
   ```

4. **Tests with coverage**
   ```
   uv run pytest
   ```

## Reporting

- If all steps pass: report "✅ All checks passed." and show the pytest summary line.
- If a step fails: show the **full output** of the failing command. Do not summarise or
  truncate errors. State clearly which step failed and what needs to be fixed before
  proceeding.

## Notes

- This is the same sequence CI runs. If it passes here, it will pass in GitHub Actions.
- The coverage threshold is 80% (set in `pyproject.toml`). Falling below this fails the run.
- If `ty check` reports errors on files you did not change, flag them as pre-existing
  and note them separately rather than silently ignoring them.
