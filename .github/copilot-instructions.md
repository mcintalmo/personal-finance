# GitHub Copilot Instructions for personal-finance

> Core project rules are in [AGENTS.md](../AGENTS.md) — the complete reference.
> This file summarises the most important conventions for Copilot's inline suggestions.

## Project

- **Package:** `personal_finance` | **Python:** 3.14+ | **Layout:** src (`src/personal_finance/`)
- **Repo:** https://github.com/mcintalmo/personal-finance

## Toolchain

Always suggest: `uv` (packages), `ruff` (lint+format), `ty` (types), `pytest` (tests).
Never suggest: pip, poetry, black, isort, flake8, mypy, pyright.

## Type Hints (required everywhere)

```python
# ✅ correct — Python 3.10+ syntax
def process(items: list[str], limit: int | None = None) -> dict[str, int]: ...

# ❌ wrong — legacy typing module
from typing import List, Optional, Dict
def process(items: List[str], limit: Optional[int] = None) -> Dict[str, int]: ...
```

## Data Models

```python
# Simple containers → dataclass
from dataclasses import dataclass

@dataclass
class Point:
    x: float
    y: float

# Validated / external data → Pydantic BaseModel
from pydantic import BaseModel

class UserCreate(BaseModel):
    name: str
    email: str

# Application settings → Pydantic BaseSettings
from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    database_url: str
    debug: bool = False

    model_config = {"env_file": ".env.local"}
```

## Before Suggesting a Commit

The code must pass all of:
```bash
uv run ruff check .
uv run ruff format --check .
uv run ty check src/
uv run pytest
```

## Environment Variables

- Local: `.env.local` (gitignored, based on `.env.example`)
- CI/CD: GitHub Actions environment secrets
- Never hardcode secrets or environment-specific values

## Commit Messages

Use Conventional Commits: `feat:`, `fix:`, `chore:`, `docs:`, `refactor:`, `test:`

## Additional Rules

See [AGENTS.md](../AGENTS.md) for the complete reference on imports, error handling,
project structure, testing conventions, and CI/CD.
