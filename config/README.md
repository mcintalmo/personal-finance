# User configuration

The pipeline is driven by four YAML files in this directory:

| File | Configures |
|------|-----------|
| `sources.yaml` | Data sources to ingest: names, account types, CSV column maps |
| `taxonomy.yaml` | The hierarchical category tree (e.g. `essentials/groceries/apples`) |
| `rules.yaml` | Deterministic regex → category rules (stage 1 of the enrichment cascade) |
| `budgets.yaml` | Budget buckets over category subtrees |

## Setup

Live files in this directory are **gitignored** — they may describe your real
accounts and finances. Start from the committed examples (dummy data):

```bash
cp config/examples/*.yaml config/
```

Then edit freely. Validation runs at load time: typos in keys, invalid regexes,
and references to category paths not defined in `taxonomy.yaml` all fail
immediately with a clear error.

All files are optional — a missing file is simply an empty section.
See `src/personal_finance/user_config.py` for the schema of each file.
