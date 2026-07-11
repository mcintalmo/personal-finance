# personal-finance

Personal finance and budget management app, using data pipelining, machine learning, AI, and data visualization to make where money is going immediately intuitive.

[![CI](https://github.com/mcintalmo/personal-finance/actions/workflows/ci.yml/badge.svg)](https://github.com/mcintalmo/personal-finance/actions/workflows/ci.yml)
[![Python 3.14+](https://img.shields.io/badge/python-3.14%2B-blue.svg)](https://python.org)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)
[![uv](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/uv/main/assets/badge/v0.json)](https://github.com/astral-sh/uv)

---

## Requirements

- [uv](https://docs.astral.sh/uv/) ≥ 0.6 — install with `curl -LsSf https://astral.sh/uv/install.sh | sh`
- Python 3.14 (uv will install it automatically)

## Setup

```bash
# Clone
git clone https://github.com/mcintalmo/personal-finance.git
cd personal-finance

# Install dependencies + dev tools
uv sync

# Install pre-commit hooks
uv run pre-commit install

# Configure local environment
cp .env.example .env.local
# Edit .env.local with your local values
```

## Development

```bash
# Run tests
uv run pytest

# Lint
uv run ruff check .

# Format
uv run ruff format .

# Type check
uv run ty check src/

# All checks (matches CI)
uv run ruff check . && uv run ruff format --check . && uv run ty check src/ && uv run pytest
```

## Project Structure

```
src/personal_finance/     # Python package (importable as `personal_finance`)
├── __init__.py
├── config.py               # Application settings (pydantic-settings)
└── exceptions.py           # Custom exception hierarchy

tests/                      # Test suite (mirrors src/ structure)
infra/terraform/            # Infrastructure as Code
.github/workflows/          # CI (ci.yml) and CD (cd.yml)
```

## CI/CD

- **CI** runs on every push and PR to `main`: ruff → ty → pytest → coverage upload.
- **CD** runs on merge to `main` and deploys to production (fill in `cd.yml` for your platform).
- **Copilot auto-review** is configured via GitHub Rulesets (see `SETUP.md`).

## AI Coding Assistants

This project ships with configuration for Claude Code (`CLAUDE.md`), Gemini CLI (`GEMINI.md`), and GitHub Copilot (`.github/copilot-instructions.md`). The shared rules live in `AGENTS.md` — the Linux Foundation cross-tool standard.

## License

MIT
