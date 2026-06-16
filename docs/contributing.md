# Contributing to tracesage

Thanks for considering a contribution. tracesage is intentionally lean — we want
every line to pull its weight.

## Setup

```bash
git clone https://github.com/kjgpta/tracesage.git
cd tracesage
python -m venv .venv
source .venv/bin/activate    # Windows: .venv\Scripts\activate
pip install -e ".[dev,langchain]"
```

## Running the tests

```bash
# Unit + integration (default — what CI runs)
pytest tests/ --ignore=tests/stress

# Stress (slow, run manually)
pytest tests/stress/ -m slow

# Single layer
pytest tests/test_database.py -v
```

## Code style

- `ruff check src/ tests/ tools/` must pass.
- Type-hint public functions.
- Pydantic v2.
- `from __future__ import annotations` at file top.
- See `CLAUDE.md` for the full convention list.

## Architecture

- `src/tracesage/` — package source
- `src/tracesage/adapters/` — framework adapters (LangChain; MCP attribution helper)
- `src/tracesage/storage/` — pluggable storage backends (SQLite + blob store)
- `src/tracesage/server/` — FastAPI routes, WebSocket, auth
- `src/tracesage/ui/` — vanilla JS + a custom SVG graph renderer (no framework)
- `src/tracesage/pytest_plugin.py` — the `tracesage_capture` fixture
- `tests/` — unit + integration; `tests/stress/` — slow tests (excluded from CI)
- `examples/` — `getting_started/` (no-key demos), `mcp/`, and the `showcase/` gallery
- `tools/` — bench, crash-recovery, other ops scripts

## Pull requests

- One concern per PR.
- Add a regression test if you're fixing a bug.
- If you're adding a feature, document it in `docs/`.
- Run `pytest tests/ --ignore=tests/stress` and `ruff check` before pushing.

## Adding a framework adapter

See [the extending guide](https://github.com/kjgpta/tracesage/blob/main/docs/extending.md).
The adapter pattern is documented;
follow LangChain's adapter as a reference.

## Reporting bugs

Please include:

- Python version
- OS
- Minimal reproducible snippet
- What you expected vs what happened
