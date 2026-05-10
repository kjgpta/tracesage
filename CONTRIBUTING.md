# Contributing to tracelens

Thanks for considering a contribution. tracelens is intentionally small in v0.1 —
we want every line to pull its weight.

## Setup

```bash
git clone https://github.com/tracelens/tracelens.git
cd tracelens
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

- `src/tracelens/` — package source
- `src/tracelens/adapters/` — framework adapters (LangChain in v0.1)
- `src/tracelens/storage/` — pluggable storage backends
- `src/tracelens/server/` — FastAPI routes, WebSocket, auth
- `src/tracelens/ui/` — vanilla JS + Cytoscape UI
- `tests/` — unit + integration
- `tests/stress/` — slow tests (excluded from CI by default)
- `examples/` — runnable demos
- `tools/` — bench, crash-recovery, other ops scripts

## Pull requests

- One concern per PR.
- Add a regression test if you're fixing a bug.
- If you're adding a feature, document it in `docs/`.
- Run `pytest tests/ --ignore=tests/stress` and `ruff check` before pushing.

## Adding a framework adapter

See [docs/extending.md](docs/extending.md). The adapter pattern is documented;
follow LangChain's adapter as a reference.

## Reporting bugs

Please include:

- Python version
- OS
- Minimal reproducible snippet
- What you expected vs what happened
