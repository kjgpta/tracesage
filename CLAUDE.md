# CLAUDE.md — tracelens project conventions
Shared context for all Claude agents working on this codebase. Read this before any change.

## Project facts

- **PyPI distribution name:** `tracelens`
- **Python import name:** `tracelens`
- **Top-level class:** `TraceLens`
- **Python:** 3.11+ (target 3.11 syntax, but allow 3.12/3.13)
- **License:** MIT
- **Package layout:** `src/tracelens/` (src layout)
- **Server bind default:** `127.0.0.1:7842`

## Code conventions

- Type-hint all public function signatures (params and return).
- `from __future__ import annotations` at the top of every `.py` with forward refs.
- Use `dict | None` over `Optional[dict]` (3.10+ syntax).
- Use `pathlib.Path`, not `os.path`.
- Use `datetime.now(timezone.utc)`, NEVER deprecated `datetime.utcnow()`.
- Pydantic v2 throughout: `model_dump()`, `model_dump_json()`, `model_validator`.
- Dataclasses for internal containers; Pydantic for API/DB boundary.

## Async conventions

- Use `asyncio.get_running_loop()`, NEVER deprecated `asyncio.get_event_loop()`.
- `asyncio.gather(*[...], return_exceptions=True)` for concurrent calls.
- Async route handlers MUST NOT call sync blocking IO directly. Use `httpx.AsyncClient`,
  `asyncio.sleep`, `aiofiles`, or `loop.run_in_executor` for blocking work.
- FastAPI startup/shutdown via `@asynccontextmanager` `lifespan` — NEVER `@app.on_event`.

## Defensive code rules (the safety guarantee)

- **The callback handler MUST NEVER raise.** Wrap every method body in `try/except Exception`,
  log to stderr, return None.
- Worker batches: per-event `try/except` inside the batch loop. One bad event must not
  abort the batch.
- WebSocket sends: catch errors per-socket; mark dead sockets and continue.
- DB write failures: log error, continue. Data loss is acceptable; agent breakage is not.

## Production safety rules

- Refuse to start if `host=0.0.0.0` and `auth_token is None`. Hard fail-stop, not warning.
- All `blob_path` values stored in DB MUST be relative paths under `base_dir`. Validate
  this on read (path-traversal guard).
- All SQL parameterized via `?` placeholders. NEVER f-string SQL.
- Bearer token auth middleware: skip ONLY `/api/health`.
- Sampling: `if random() > sample_rate: return` at the top of every callback method
  (after `run_id` extraction so root-run tracking stays consistent).

## Pydantic v2 specifics

```python
# Correct:
class Foo(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)
    bar: dict[str, int] = Field(default_factory=dict)

# WRONG (v1 syntax):
class Foo(BaseModel):
    class Config:
        arbitrary_types_allowed = True
```

## File ownership boundaries (multi-agent coordination)

| Owner | Files |
|---|---|
| Orchestrator | `pyproject.toml`, `src/tracelens/__init__.py`, `models.py`, `config.py`, `storage/backend.py` (Protocol only), `tests/conftest.py`, `tests/integration/**`, `cli.py`, `examples/**`, docs |
| **Agent A (Storage)** | `storage/sqlite_backend.py`, `storage/blob_store.py`, `storage/__init__.py` re-exports, `tests/test_database.py`, `tests/test_blob_store.py` |
| **Agent B (Pipeline)** | `tracer.py`, `worker.py`, `adapters/__init__.py`, `adapters/langchain.py`, `tests/test_tracer.py`, `tests/test_worker.py`, `tests/test_handler.py` |
| **Agent C (Server)** | `server/__init__.py`, `server/app.py`, `server/rest.py`, `server/ws.py`, `server/auth.py`, `tests/test_server.py` |
| **Agent D (UI)** | `src/tracelens/ui/index.html`, `ui/app.js`, `ui/graph.js`, `ui/styles.css` |

If you need to edit a file owned by another agent, STOP and surface the conflict.

## Testing conventions

- `pytest` + `pytest-asyncio` with `asyncio_mode = "auto"` (configured in `pyproject.toml`).
- Tests live alongside the package: `tests/test_<module>.py`.
- Integration tests in `tests/integration/` — use `langchain_core.language_models.fake.FakeListChatModel`.
- Use `tmp_path` and `tmp_data_dir` fixtures from `conftest.py` for test isolation.
- After edits, run `python -m py_compile <file>` to catch parse errors.
- Run `pytest tests/test_<your_module>.py` before declaring done.

## Done means

- `python -m py_compile` passes on every modified file.
- `ruff check` passes.
- `pytest <your test file>` passes.
- No `ResourceWarning` during tests (no leaked DB connections, async tasks).
- Public API matches the contract in your brief exactly — no signature drift.
