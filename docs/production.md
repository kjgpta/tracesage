# Running tracesage in production

`tracesage` is a single-process embedded library — **local-first**, not a hosted
production-monitoring service. This guide covers the knobs and safety rails for running
it beyond your laptop (auth, sampling, retention) plus exporting to a real production
observability stack via OpenTelemetry. It's architected so future versions can split the
producer (your app) from the consumer (a centralized trace server).

## Defaults that matter

- Binds `127.0.0.1` only. Set `auth_token` if you want to expose the UI.
- `sample_rate=1.0` — capture every event. Lower in high-volume production.
- `per_run_event_cap=50000` — single runaway run won't fill the queue.
- Queue drops events instead of blocking the user's pipeline. Counter exposed in `/api/stats`.

## Turning tracesage off (kill switch)

Wire tracesage in once and disable it per-environment — no code change:

```bash
export TRACESAGE_ENABLED=false      # prod: complete no-op
```

When disabled, `TraceSage.create()` / `tracesage.trace()` / `TraceSage.session()`
return an **inert tracer**: no embedded server is started, no DB/worker/queue is
created, `.handler` is a no-op callback, and overhead is near zero. Your
`callbacks=[tracer.handler]` (or global `install()`) wiring keeps working untouched —
it just does nothing. This is the recommended way to ship the integration in code but
keep it out of an environment that doesn't want it.

**Overhead when disabled** (measured against a near-instant fake LLM, so it's a
worst-case microbenchmark — real LLM latency dwarfs all of these):

- **Startup:** `TraceSage.create()` returns in ~0.3 ms and binds nothing — no port, no
  DB file, no worker task, no background thread.
- **Per call, global-install pattern** (`tracesage.trace()` / `install=True`):
  effectively zero — `install()` is a no-op, so nothing is registered with LangChain
  and your calls take the same path as if tracesage weren't there.
- **Per call, explicit `callbacks=[tracer.handler]` pattern:** a small *fixed* cost
  (~14 µs/call in the microbenchmark) from LangChain building a callback manager and
  doing one skip-check per event. It does not scale with your workload; against a real
  LLM call (hundreds of ms) it is < 0.01%. The disabled handler sets every LangChain
  `ignore_*` flag, so its methods are never actually invoked.

For literal zero overhead with the same code in every environment, prefer the
global-install pattern and toggle `TRACESAGE_ENABLED`.

## Recommended production config

If you DO want tracing in prod but not the dev conveniences:

```python
from tracesage import TraceSage, TraceSageConfig

cfg = TraceSageConfig(
    enabled=True,
    start_server=False,     # don't run the embedded UI server in your app process
    print_run_url=False,    # no stderr trace-link spam
    sample_rate=0.1,        # capture a slice (see Sampling guidance below)
)
tracer = await TraceSage.create(config=cfg)   # config drives start_server
```

Every field above has a `TRACESAGE_*` env var (`TRACESAGE_START_SERVER=false`,
`TRACESAGE_PRINT_RUN_URL=false`, `TRACESAGE_SAMPLE_RATE=0.1`, …), so you can ship the same
code and tune it per environment without editing it. That includes
`tracesage.trace()` and `tracesage.start()`, which follow `TRACESAGE_START_SERVER`
the same way `create()` does. A `start_server=` kwarg on
`create()` / `session()` / `start()` / `trace()` overrides the config when you
need to force it.

**Long-running async service (e.g. FastAPI).** Create the tracer once at startup and reuse
its handler across requests; stop it on shutdown so the queue drains:

```python
from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app):
    app.state.tracer = await TraceSage.create(config=cfg)
    try:
        yield
    finally:
        await app.state.tracer.stop()   # drains the queue, closes the DB
```

Then pass `config={"callbacks": [app.state.tracer.handler]}` on each `ainvoke`, or call
`app.state.tracer.install()` once after create to capture globally. (The `async with
TraceSage.session(...)` context manager is for scripts/one-shot runs — it tears down at
block exit.)

Then view the data out-of-band with `tracesage serve --data-dir <dir>` (or point it at
durable storage). The embedded SQLite + local blobs are built for a single process
or a single node. To collect from many processes, run one `tracesage serve --ingest`
collector and point them at it over OTLP, or export to your existing backend
([below](#exporting-to-a-central-observability-stack-opentelemetry)).

## Sampling guidance

| Volume | Recommended sample_rate |
|---|---|
| < 100 runs/day | 1.0 (capture everything) |
| 100–10,000 runs/day | 0.5 to 1.0 |
| 10,000–100,000 runs/day | 0.1 to 0.5 |
| > 100,000 runs/day | 0.01 to 0.1, plus export to a central backend via OTLP (below) |

Sampling is per-root-run, so partial traces never happen.

## Exporting to a central observability stack (OpenTelemetry)

For multi-process or production deployments, set `otlp_endpoint` to also stream every
trace as OpenTelemetry spans to your collector / Tempo / Jaeger / Datadog / Honeycomb —
the local SQLite store remains for the dev view, while the OTLP backend handles
centralization, retention, and cross-service correlation:

```bash
pip install "tracesage[otel]"
export TRACESAGE_OTLP_ENDPOINT=http://otel-collector:4318
```

Export is best-effort and never blocks or breaks the app if the collector is down.
See [Configuration → OpenTelemetry export](configuration.md) for the full mapping.

## Authentication

Set `auth_token` (any random string ≥ 32 chars) and the server enforces:

- HTTP: `Authorization: Bearer <token>` on every endpoint except `/api/health`.
- WebSocket: `Sec-WebSocket-Protocol: tracesage.v1, tracesage.bearer.<base64url(token)>`
  (legacy `?token=` still works but leaks the token into URLs and logs).

Constant-time comparison via `hmac.compare_digest` prevents timing leaks.

```bash
export TRACESAGE_HOST=0.0.0.0
export TRACESAGE_AUTH_TOKEN=$(openssl rand -hex 32)
```

`tracesage` will refuse to start if `host` is non-loopback and `auth_token` is unset.

## TLS

`tracesage` does not terminate TLS. Run behind a reverse proxy (nginx, Caddy,
Traefik, AWS ALB). Example nginx:

```nginx
location /tracesage/ {
    proxy_pass http://127.0.0.1:7842/;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_set_header Host $host;
}
```

The UI uses relative URLs so prefix paths work.

## Retention

The `runs` and `events` tables grow without bound by default. To enforce a
retention cap:

```bash
# Daily cron / systemd timer
tracesage gc --max-runs 50000
```

This deletes the oldest runs and their blobs, in FIFO order.

## Backup

The data directory is fully portable:

```bash
rsync -a --delete ~/.tracesage/ backup-host:~/tracesage_$(date +%F)/
```

Include `traces.db`, `traces.db-wal`, `traces.db-shm`, and the `blobs/` directory.

## What to monitor (the watcher's watcher)

`GET /api/stats` returns:

- `events_processed` — total events written to DB. Should grow steadily.
- `events_dropped` — should be `0` in healthy state. If non-zero, your queue is full —
  increase `queue_maxsize` or reduce `sample_rate`.
- `events_sampled_out` — counter of events skipped by sampling. Informational.
- `runs_throttled` — counter of runs that hit `per_run_event_cap`. Investigate runaway agents.
- `last_write_latency_ms` — most recent batch write latency.
- `p99_write_latency_ms` — rolling p99 over last 1000 batches. Should be < 500 ms typical.
- `queue_depth` — current queue size. Should hover near 0.

## Performance characteristics

Bench (Linux x86, NVMe SSD, ~5K events with 20% blob-eligible):
- Sustained: 800–1200 ev/s
- p99 write latency: 80–150 ms
- 0 dropped under sustained load

Bench (Windows NTFS, same machine):
- Sustained: 60–100 ev/s (gzip + fsync amplification)
- p99 write latency: 1000–2000 ms
- 0 dropped under sustained load

If you need more throughput on Windows, raise `worker_batch_size` to 200 and
`worker_batch_timeout` to 0.5.

## Troubleshooting

- **"Refusing to start: 0.0.0.0 without TRACESAGE_AUTH_TOKEN"** — set the env var.
- **"Queue drain timed out"** at shutdown — your worker fell behind; some events lost.
  Raise `queue_maxsize` or shutdown with more time.
- **"FOREIGN KEY constraint failed"** in worker logs — usually means your LangGraph
  emitted events for a run_id that wasn't preceded by `chain_start`. Worker auto-creates
  the runs row but logs the issue.
- **Events visible but no agent_name** — your custom Runnable doesn't supply a name.
  Pass `with_config(run_name="MyAgent")` on the Runnable.

## Upgrade path

Within 1.x the data directory stays readable: a newer 1.x opens a directory written
by an older 1.x, and `tracesage serve` against that directory shows the old runs.
To collect from many processes, run one `tracesage serve --ingest` and point them at
it over OTLP, or export to your existing backend
([above](#exporting-to-a-central-observability-stack-opentelemetry)).

## Stability and versioning

From v1.0.0 tracesage follows [semantic versioning](https://semver.org): patch releases
fix bugs, minor releases add features backwards-compatibly, and anything below that can
break your code or data only changes in a **major** release.

Covered by that promise:

- **Python API** — everything in `tracesage.__all__` (`TraceSage`, `TraceSageConfig`,
  `trace`, `start`, `instrument`, `Span`, `current_span`, …) and the public (non-`_`)
  methods and properties of `TraceSage` / `BackgroundTracer` / `Span`, including
  `span()`, `trace_fn()`, `instrument()`, `install_otel()`, `handler`, `flush()`,
  `stop()`, `run_url()`; plus `tracesage.ingest.TraceSageSpanExporter` / `install_otel`.
- **Configuration** — every `TRACESAGE_*` setting in [Configuration](configuration.md):
  names, types and defaults (a default may only change in a major, or to fix a security
  issue).
- **HTTP** — the REST paths under `/api` (including `/api/search`, `/api/threads`,
  `/api/runs/{id}/spans`, notes and tags), the WebSocket paths under `/ws`,
  `POST /v1/traces`, `POST /v1/hooks/{app}` and `POST /v1/logs`, and the documented
  request/response fields in the [API reference](api.md). New fields may be added to
  responses in a minor release; existing ones are not removed or retyped.
- **CLI** — command names, flags and exit codes in the [CLI reference](cli.md),
  including `search`, `thread`, `reindex`, `connect`, `disconnect`, `service` and
  `mcp`, and the `--json` / JSONL export formats.
- **MCP tools** — the tool and prompt names served by `tracesage mcp`: `list_runs`,
  `get_run`, `get_run_spans`, `get_step`, `search`, `list_errors`, `list_threads`,
  `get_thread`, `get_stats`, `get_topology`, `compare_runs`, `add_note`, `set_tags`,
  `delete_run`, and the `debug_failed_run` prompt.
- **Data directory** — SQLite schema migrations are forward-only and automatic: a newer
  1.x always opens a data dir written by an older 1.x. Blobs stay gzipped JSON.
- **Event model** — `EventType` values, `RunStatus` values and the `Run` / `StoredEvent`
  fields documented in the API reference.

Not covered (may change in any release):

- Names starting with `_`, and modules not mentioned above (internals of `tracesage.worker`,
  `tracesage.storage.*`, `tracesage.ingest.mapper`, …).
- The bundled UI's HTML/JS/CSS and its look; screenshots in the docs.
- The exact text of step summaries, log messages and error messages.
- Undocumented payload keys and `tracesage.*` OpenTelemetry attributes beyond those listed
  in [Configuration → OpenTelemetry export](configuration.md#opentelemetry-export).
- Which upstream OpenTelemetry instrumentation package an `instrument(...)` name installs,
  and the span shapes those packages produce (they follow their own release cycles).

**Deprecation policy.** Anything covered above is deprecated before removal: it keeps
working and emits a `DeprecationWarning` (or a CLI warning) for at least one minor
release, the changelog lists it under *Deprecated*, and it is removed only in the next
major. Security fixes are the one exception and are called out in the changelog.

Supported Python versions follow the classifiers in `pyproject.toml`; dropping a Python
version that is still upstream-supported is treated as a breaking change.
