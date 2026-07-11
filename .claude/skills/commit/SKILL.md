---
description: >
  Stage all changes, run pre-flight checks, and create a Conventional Commits message.
  Use when asked to commit, save changes, or create a git commit.
disable-model-invocation: true
allowed-tools: Bash(uv run *) Bash(git status*) Bash(git diff *) Bash(git add *) Bash(git commit *)
---

Stage and commit the current changes. Follow each step in order.

## 1. Pre-flight: clean up code

Run format and lint before committing so the commit is clean:

```
uv run ruff format . && uv run ruff check . --fix
```

If ruff made changes, those changes will be included in the commit.

## 2. Inspect what is changing

```
git status
git diff --cached   # staged changes
git diff            # unstaged changes
```

Understand the full scope of what will be committed before writing the message.

## 3. Stage

Unless the user specified particular files, stage everything:

```
git add -A
```

## 4. Write the commit message

Use [Conventional Commits](https://www.conventionalcommits.org/) format:

```
type(scope): short imperative description
```

**Types:**

| Type | When to use |
|------|-------------|
| `feat` | A new feature or capability |
| `fix` | A bug fix |
| `refactor` | Code restructure with no behaviour change |
| `test` | Adding or updating tests only |
| `docs` | Documentation changes only |
| `chore` | Maintenance: deps, tooling, config |
| `ci` | CI/CD workflow changes |
| `build` | Build system changes |
| `perf` | Performance improvements |

**Rules:**
- Subject line: imperative mood ("add", not "added"), ≤ 72 characters, no trailing period
- Scope: optional, the module or layer affected (e.g. `config`, `auth`, `api`)
- Body: explain *why* — not what (the diff shows what). Wrap at 72 chars.
- Breaking changes: add `BREAKING CHANGE: <description>` in the footer

**Good examples:**
```
feat(config): add staging environment support
fix(api): handle None return from database query
chore: upgrade ruff to 0.16 and update pre-commit hooks
test(config): cover is_production property
```

**Bad examples:**
```
minor fixes          # not conventional, not descriptive
WIP                  # not a commit message
updated stuff        # vague, past tense
```

## 5. Commit

```
git commit -m "<subject>" -m "<body if needed>"
```

After committing, show the one-line summary: commit hash + subject.
