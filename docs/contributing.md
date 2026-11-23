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

## Checking dependencies for CVEs

`pip-audit` (in the `dev` extra) checks installed packages against the PyPI and
OSV advisory databases:

```bash
pip-audit --skip-editable          # everything installed in your venv
```

The `Dependencies` CI workflow also audits, and runs the tests on, the *oldest*
versions `pyproject.toml` allows. Reproduce it locally with
[uv](https://docs.astral.sh/uv/):

```bash
uv venv /tmp/lowest --python 3.11
VIRTUAL_ENV=/tmp/lowest uv pip install --resolution lowest-direct -e ".[dev,langchain,mcp,otel]"
VIRTUAL_ENV=/tmp/lowest uv pip freeze | grep -v -e '^-e ' -e '^tracesage' > /tmp/lowest.txt
pip-audit -r /tmp/lowest.txt --no-deps --disable-pip
/tmp/lowest/bin/python -m pytest tests/ --ignore=tests/stress -q
```

If a floor is flagged, raise it to the fixed version in `pyproject.toml`.

## Code style

- `ruff check src/ tests/ tools/` must pass.
- Type-hint public functions; use `dict | None`, not `Optional[dict]`.
- `from __future__ import annotations` at file top.
- Pydantic v2 (`model_dump()`, `ConfigDict`) at API / DB boundaries; dataclasses for
  internal containers.
- `pathlib.Path`, not `os.path`; `datetime.now(timezone.utc)`, never `utcnow()`.
- Async code uses `asyncio.get_running_loop()` and never blocks the event loop with
  sync IO. FastAPI startup / shutdown goes through `lifespan`, not `@app.on_event`.
- SQL is always parameterized with `?` placeholders.

### Safety rules

tracesage runs inside other people's agents, so tracing must never break them:

- The callback handler and exporters never raise. Catch, log to stderr, return.
- One bad event must not abort a batch; failed DB writes are logged and skipped.
- Everything ingested (callbacks, OTLP, AI-app hooks) goes through `TraceSage.emit`,
  so sampling, the per-run cap and redaction always apply.
- `/api/*`, `/ws/*` and `/v1/*` stay behind the bearer token when one is set, and the
  server refuses to bind a non-loopback host without one.

## Commit style

Commits follow [Conventional Commits](https://www.conventionalcommits.org):
`type(scope): summary`, for example `fix(examples): handle a missing 'next' key` or
`docs(readme): cover all CLI commands`. Common types are `feat`, `fix`, `docs`,
`test`, `refactor`, `ci` and `chore`. Keep the summary in the imperative and under
about 72 characters.

## Architecture

- `src/tracesage/` — package source
- `src/tracesage/adapters/` — framework adapters (LangChain; MCP attribution helper)
- `src/tracesage/storage/` — pluggable storage backends (SQLite + blob store)
- `src/tracesage/server/` — FastAPI routes, WebSocket, auth
- `src/tracesage/ui/` — vanilla JS + a custom SVG graph renderer (no framework)
- `src/tracesage/pytest_plugin.py` — the `tracesage_capture` fixture
- `tests/` — unit + integration; `tests/stress/` — slow tests (excluded from CI)
- `examples/` — `getting_started/` (no-key demos), `mcp/`, and the `showcase/` gallery
- `tools/` — `bench.py` (the README performance numbers), `crash_recovery_test.py`,
  and `mcpb/` (the Claude Desktop extension built by `tracesage connect --mcpb`)

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
