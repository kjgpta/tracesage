# Extending tracesage

Three planned extension points. tracesage currently ships the LangChain adapter; the
protocols for the others are stable.

## Adding a new framework adapter

tracesage's core is framework-neutral. The LangChain adapter is one
concrete implementation of the same pattern.

### Pattern

1. Create `src/tracesage/adapters/<framework>.py`.
2. Subclass that framework's callback/hook base class.
3. Wrap every method body in `try/except Exception` (the safety contract).
4. Construct a `RawEvent` with the right `EventType` and call `tracer.emit(event)`.
5. Use `tracer.get_or_set_root(run_id, parent_run_id)` to track nested runs.

### Skeleton

```python
from tracesage.models import EventType, RawEvent
from tracesage.tracer import TraceSage
import uuid
from datetime import UTC, datetime

class MyFrameworkHandler:
    def __init__(self, tracer: TraceSage):
        self._tracer = tracer

    def on_step_start(self, *, run_id, parent_run_id=None, name=None, **kwargs):
        try:
            run_id_s = str(run_id)
            parent_s = str(parent_run_id) if parent_run_id else None
            root = self._tracer.get_or_set_root(run_id_s, parent_s)
            event = RawEvent(
                event_id=str(uuid.uuid4()),
                event_type=EventType.CHAIN_START,
                run_id=run_id_s,
                parent_run_id=parent_s,
                root_run_id=root,
                timestamp=datetime.now(UTC),
                agent_name=name,
                summary=f"{name}: starting",
                full_blob_eligible=False,
            )
            self._tracer.emit(event)
        except Exception:
            # NEVER raise; logging optional
            pass
```

### Required event types

To make the UI work, your adapter should emit at minimum:
- `RUN_START` (synthetic, on root invocation)
- `CHAIN_START` / `CHAIN_END` for each major step
- `LLM_END` for LLM calls (with `token_input`/`token_output`)

Optional but improves the experience:
- `TOOL_START` / `TOOL_END`
- `RETRIEVER_START` / `RETRIEVER_END`
- `*_ERROR` variants

### Adapters on the roadmap

- v0.4: CrewAI
- v0.5: AutoGen
- v0.6: LlamaIndex
- v0.7: Semantic Kernel
- Community PRs welcome

## Adding a storage backend

`StorageBackend` (in `src/tracesage/storage/backend.py`) is a `Protocol` with
~12 methods. tracesage ships `SQLiteBackend`. Planned backends:

- `PostgresBackend` — for centralized multi-process deployments
- `JSONLBackend` — append-only files, no DB, easier portability
- `RemoteHTTPBackend` — producer ships events over HTTP to a separate trace server

Implementing one means satisfying every method of `StorageBackend` exactly.
Test against `tests/test_database.py` (rename and adjust the fixtures).

## Replacing the UI

The UI is loaded as static files at `/ui/`. To swap:

1. Build your replacement (any framework).
2. Replace files in `src/tracesage/ui/`.
3. The HTTP API contract is documented in [docs/api.md](api.md).

The current UI is vanilla JS with a hand-written SVG graph renderer (no framework), with no build
step required. A React rewrite is on the roadmap.
