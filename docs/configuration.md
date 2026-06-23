# Configuration

All settings live on `TraceSageConfig` (Pydantic Settings). Override via:

1. Constructor kwargs: `TraceSageConfig(port=8000)`
2. Environment variables prefixed `TRACESAGE_`: `TRACESAGE_PORT=8000`

## Settings reference

| Setting | Env var | Default | Notes |
|---|---|---|---|
| `enabled` | `TRACESAGE_ENABLED` | `True` | **Kill switch.** `False` returns an inert tracer — no server, no DB/worker, a no-op handler, near-zero overhead. See [Disabling](#disabling-kill-switch). |
| `host` | `TRACESAGE_HOST` | `127.0.0.1` | Bind address. Refuses non-loopback without `auth_token`. |
| `port` | `TRACESAGE_PORT` | `7842` | `0` = ephemeral; actual bound port readable via `tracer.bound_port` / `tracer.ui_url`. |
| `port_auto` | `TRACESAGE_PORT_AUTO` | `True` | If `port` is busy, auto-bind the next free port (scan up from `port`, then ephemeral). Set `False` to use exactly `port`. |
| `project_name` | `TRACESAGE_PROJECT_NAME` | `None` | Optional label for this app, shown in the UI header + browser tab so you can tell apart multiple apps' UIs. Unset = nothing shown. |
| `auth_token` | `TRACESAGE_AUTH_TOKEN` | `None` | Bearer token for HTTP + WebSocket auth. |
| `public_url` | `TRACESAGE_PUBLIC_URL` | `None` | Base URL used for run deep-links (e.g. behind a reverse proxy). Falls back to the bound host/port. |
| `print_run_url` | `TRACESAGE_PRINT_RUN_URL` | `True` | Print a `🔍 tracesage: <url>` link to stderr on each new root run. Set `False` in noisy/prod environments. |
| `start_server` | `TRACESAGE_START_SERVER` | `True` | Start the embedded UI server in-process. Set `False` in prod to keep capturing without a web server (view later with `tracesage serve`). A `start_server=` kwarg to `create()`/`session()` overrides this. |
| `cors_origins` | `TRACESAGE_CORS_ORIGINS` | `["*"]` | Allowed CORS origins. Tighten to an explicit allowlist when exposing beyond localhost. |
| `startup_health_timeout_s` | `TRACESAGE_STARTUP_HEALTH_TIMEOUT_S` | `3.0` | How long `create()` waits for the embedded server to report started. |
| `data_dir` | `TRACESAGE_DATA_DIR` | `~/.tracesage` | Root data directory (DB + blobs). |
| `db_filename` | `TRACESAGE_DB_FILENAME` | `traces.db` | SQLite filename inside `data_dir`. |
| `blob_subdir` | `TRACESAGE_BLOB_SUBDIR` | `blobs` | Blob subdirectory inside `data_dir`. |
| `db_pool_size` | `TRACESAGE_DB_POOL_SIZE` | `5` | Max concurrent DB connections. |
| `queue_maxsize` | `TRACESAGE_QUEUE_MAXSIZE` | `50000` | Drop new events if exceeded; counter exposed in `/api/stats`. |
| `worker_batch_size` | `TRACESAGE_WORKER_BATCH_SIZE` | `50` | Max events per DB transaction. |
| `worker_batch_timeout` | `TRACESAGE_WORKER_BATCH_TIMEOUT` | `0.1` | Seconds; how long the worker waits for a full batch before flushing. |
| `sample_rate` | `TRACESAGE_SAMPLE_RATE` | `1.0` | `0.0–1.0`; per-run sampling decided once per root. |
| `per_run_event_cap` | `TRACESAGE_PER_RUN_EVENT_CAP` | `50000` | Circuit breaker per run. |
| `summary_max_chars` | `TRACESAGE_SUMMARY_MAX_CHARS` | `500` | Max summary length stored in `events.summary`. |
| `max_runs` | `TRACESAGE_MAX_RUNS` | `10000` | Retention cap; enforce via `tracesage gc`. |
| `max_blob_size_gb` | `TRACESAGE_MAX_BLOB_SIZE_GB` | `10.0` | Retention target; enforced by `tracesage gc --max-blob-size-gb`. |
| `redact_patterns` | `TRACESAGE_REDACT_PATTERNS` | `[]` | Opt-in: regex patterns scrubbed from summaries + payloads before storage. Empty = off. |
| `redact_replacement` | `TRACESAGE_REDACT_REPLACEMENT` | `[REDACTED]` | String that matched redaction patterns are replaced with. |
| `log_level` | `TRACESAGE_LOG_LEVEL` | `WARNING` | Python logging level. |
| `otlp_endpoint` | `TRACESAGE_OTLP_ENDPOINT` | `None` | OTLP/HTTP endpoint for OpenTelemetry export (e.g. `http://localhost:4318`). When set, events are also exported as OTel spans. Requires the `tracesage[otel]` extra. |
| `otlp_service_name` | `TRACESAGE_OTLP_SERVICE_NAME` | `tracesage` | `service.name` resource attribute on exported spans. |
| `otlp_headers` | `TRACESAGE_OTLP_HEADERS` | `{}` | Extra OTLP headers (e.g. a SaaS API key like `x-honeycomb-team`). |

## Disabling (kill switch)

Set `enabled=False` (or `TRACESAGE_ENABLED=false`) to make tracesage a complete
no-op: `TraceSage.create()` / `tracesage.trace()` / `TraceSage.session()` return an
inert tracer with no embedded server, no DB/worker/queue, and a no-op callback handler.
This lets you wire tracesage in once and switch it off per-environment (e.g. in prod)
without touching your integration code. The bind-safety fail-stop below is skipped when
disabled (nothing binds). See [production.md](production.md) for the measured overhead
when disabled.

```bash
export TRACESAGE_ENABLED=false      # complete no-op
```

## Isolating multiple applications

`data_dir` is the isolation boundary. All cross-run views — the **topology map**
and the **"Tools by source"** panel — are computed *per data dir*, aggregating
every run stored there. So if two different applications write to the **same**
`data_dir` (e.g. both use the default `~/.tracesage`), their graphs and tool
inventories **merge** and appear to interfere.

To keep applications separate, give each its own `data_dir`:

```python
# Application A
await TraceSage.create(TraceSageConfig(data_dir="~/.tracesage/app-a"))
# Application B
await TraceSage.create(TraceSageConfig(data_dir="~/.tracesage/app-b"))
```

```bash
# inspect each independently — topology/tools are scoped to that app's dir
tracesage runs    -d ~/.tracesage/app-a
tracesage serve   -d ~/.tracesage/app-b
```

The same applies to the env var (`TRACESAGE_DATA_DIR=~/.tracesage/app-a`). Runs in
different data dirs never appear in each other's lists, topology, or tool inventory.

### Running several apps at once

When you run two apps simultaneously, three things keep their UIs separate without
manual fiddling:

- **Auto-port** (`port_auto`, default on): the first app takes `7842`, the second
  auto-binds `7843`, etc. The real URL is printed on startup (`tracer.ui_url`); no
  more "port already in use".
- **Project name** (`TRACESAGE_PROJECT_NAME`): label each app so its UI header + tab
  title say which one it is.
- **Data dir** (`data_dir`, above): keeps each app's runs/topology/tools separate.

```bash
# terminal 1
TRACESAGE_PROJECT_NAME=app-a TRACESAGE_DATA_DIR=~/.tracesage/app-a python app_a.py
# terminal 2 — auto-lands on 7843, header reads "app-b"
TRACESAGE_PROJECT_NAME=app-b TRACESAGE_DATA_DIR=~/.tracesage/app-b python app_b.py
```

## OpenTelemetry export

tracesage's local UI/CLI is the developer-loop view. To also feed agent traces into
a production observability stack, enable **OpenTelemetry (OTLP) export** — every event
is emitted as an OTel span *in addition to* the local SQLite store, so the data lands
in any OTLP-compatible backend (OTel Collector, Grafana Tempo, Jaeger, Datadog,
Honeycomb, Arize/Phoenix, …) with no vendor lock-in.

```bash
pip install "tracesage[otel]"
```

```python
from tracesage import TraceSage, TraceSageConfig

await TraceSage.create(TraceSageConfig(
    otlp_endpoint="http://localhost:4318",     # "/v1/traces" is appended if absent
    otlp_service_name="my-agent",
    otlp_headers={"x-honeycomb-team": "..."},  # optional, for SaaS backends
))
```

Or purely via env vars (no code change):

```bash
export TRACESAGE_OTLP_ENDPOINT=http://localhost:4318
```

Span mapping: `root_run_id` → trace, `run_id` → span, `parent_run_id` → parent span;
start/end timestamps become the span's duration; tokens (`gen_ai.usage.*`), the tool/
agent name, MCP server, and errors become span attributes/status. Export is
**best-effort** — if the `[otel]` extra is missing or the collector is unreachable,
tracing continues and your application is unaffected.

### Viewing the exported spans

OTel export is **config-driven, not a UI toggle** — there is no button in tracesage's
own UI. tracesage's UI keeps showing the local SQLite view; the exported spans show up
in **your OTel backend's** UI. You need a listener on the OTLP port (`4318` for HTTP):

=== "Jaeger (web UI, needs Docker)"

    ```bash
    # 1. Start Jaeger (OTLP receiver on 4318, UI on 16686)
    docker run --rm -p 16686:16686 -p 4318:4318 jaegertracing/all-in-one:latest

    # 2. In another terminal, run your app with export on
    pip install "tracesage[otel]"
    export TRACESAGE_OTLP_ENDPOINT=http://localhost:4318
    export TRACESAGE_OTLP_SERVICE_NAME=my-agent
    python examples/mcp/main.py

    # 3. Open http://localhost:16686 → pick service "my-agent" → Find Traces
    ```

=== "otel-tui (terminal, no Docker)"

    ```bash
    brew install ymtdzzz/tap/otel-tui   # or grab a release binary
    otel-tui                            # listens on :4318 and shows traces live

    # then, in another terminal:
    pip install "tracesage[otel]"
    export TRACESAGE_OTLP_ENDPOINT=http://localhost:4318
    python examples/mcp/main.py
    ```

The MCP example also accepts an `--otlp` convenience flag instead of the env var
(`TRACESAGE_OTLP_ENDPOINT` works for every example since config reads env vars):

```bash
python examples/mcp/main.py --otlp http://localhost:4318
```

You'll see the run as a trace tree — `chain LangGraph` → `chain weather_agent` →
`tool get_weather` (with `tracesage.mcp_server=weather`), etc. — with durations,
token counts, and any errors as span attributes. Without a listener on `:4318`, spans
are emitted but go nowhere (you'll just see connection-refused warnings) — tracing
and the local UI still work.

## Production safety rails

- `host=0.0.0.0` (or any non-loopback) **without** `auth_token` is a hard fail-stop:
  `TraceSageConfig` raises `ValueError` at construction (unless `enabled=False`).
- `sample_rate` outside `[0, 1]` raises.
- All numeric caps must be positive.

## Sampling behavior

Sampling is decided **deterministically per root run**: a stable hash of the
root `run_id` is compared against `sample_rate`, so the same root always yields
the same keep/drop verdict (no mid-run flip even if internal state is evicted).
If kept, every descendant event of that root is captured; if dropped, every
descendant is dropped. This avoids half-traced runs. Note the decision depends
only on the hash for a fixed `sample_rate`.

## Per-run event cap (circuit breaker)

When a single run exceeds `per_run_event_cap` events (default 50K), the
tracer marks it `throttled` and silently drops further events for that run.
Other runs are unaffected. Visible via `stats.runs_throttled`. The run's
`status` is unchanged (still `running`).

## Redaction

Set `redact_patterns` to a list of regexes to scrub secrets/PII from event
summaries and full payloads **before** they are written to the DB/blob or
broadcast over WebSocket. It is opt-in (empty by default), applied in the
tracer's `emit()` path, and fails closed — if redaction errors on a payload,
that field is dropped rather than stored unredacted.

```python
TraceSageConfig(redact_patterns=[r"sk-[A-Za-z0-9]{20,}", r"\b\d{16}\b"])
```

## Blob retention

Blobs grow with `chain_end` / `llm_end` / `tool_end` / `agent_finish` /
`retriever_end` events. Periodically run `tracesage gc` to trim oldest runs and
their blobs — by count (`--max-runs 5000`) and/or by total size
(`--max-blob-size-gb 5`).

## Cross-machine deployment

The `data_dir` is portable — `rsync` or copy it to another host, then run
`tracesage serve --data-dir <path>` to view it remotely. The `traces.db`
file (plus `-wal` / `-shm` siblings) and `blobs/` directory are all you need.
