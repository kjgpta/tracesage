# Running tracelens in production

`tracelens` is a single-process embedded library. It is suitable for
production monitoring of a single Python application, and architected so future
versions can split the producer (your app) from the consumer (a centralized
trace server).

## Defaults that matter

- Binds `127.0.0.1` only. Set `auth_token` if you want to expose the UI.
- `sample_rate=1.0` — capture every event. Lower in high-volume production.
- `per_run_event_cap=50000` — single runaway run won't fill the queue.
- Queue drops events instead of blocking the user's pipeline. Counter exposed in `/api/stats`.

## Turning tracelens off (kill switch)

Wire tracelens in once and disable it per-environment — no code change:

```bash
export TRACELENS_ENABLED=false      # prod: complete no-op
```

When disabled, `TraceLens.create()` / `tracelens.trace()` / `TraceLens.session()`
return an **inert tracer**: no embedded server is started, no DB/worker/queue is
created, `.handler` is a no-op callback, and overhead is near zero. Your
`callbacks=[tracer.handler]` (or global `install()`) wiring keeps working untouched —
it just does nothing. This is the recommended way to ship the integration in code but
keep it out of an environment that doesn't want it.

**Overhead when disabled** (measured against a near-instant fake LLM, so it's a
worst-case microbenchmark — real LLM latency dwarfs all of these):

- **Startup:** `TraceLens.create()` returns in ~0.3 ms and binds nothing — no port, no
  DB file, no worker task, no background thread.
- **Per call, global-install pattern** (`tracelens.trace()` / `install=True`):
  effectively zero — `install()` is a no-op, so nothing is registered with LangChain
  and your calls take the same path as if tracelens weren't there.
- **Per call, explicit `callbacks=[tracer.handler]` pattern:** a small *fixed* cost
  (~14 µs/call in the microbenchmark) from LangChain building a callback manager and
  doing one skip-check per event. It does not scale with your workload; against a real
  LLM call (hundreds of ms) it is < 0.01%. The disabled handler sets every LangChain
  `ignore_*` flag, so its methods are never actually invoked.

For literal zero overhead with the same code in every environment, prefer the
global-install pattern and toggle `TRACELENS_ENABLED`.

## Recommended production config

If you DO want tracing in prod but not the dev conveniences:

```python
from tracelens import TraceLens, TraceLensConfig

cfg = TraceLensConfig(
    enabled=True,
    start_server=False,     # don't run the embedded UI server in your app process
    print_run_url=False,    # no stderr trace-link spam
    sample_rate=0.1,        # capture a slice (see Sampling guidance below)
)
tracer = await TraceLens.create(config=cfg)   # config drives start_server
```

Every field above has a `TRACELENS_*` env var (`TRACELENS_START_SERVER=false`,
`TRACELENS_PRINT_RUN_URL=false`, `TRACELENS_SAMPLE_RATE=0.1`, …), so you can ship the same
code and tune it per environment without editing it. A `start_server=` kwarg to
`create()`/`session()` overrides the config when you need to force it.

**Long-running async service (e.g. FastAPI).** Create the tracer once at startup and reuse
its handler across requests; stop it on shutdown so the queue drains:

```python
from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app):
    app.state.tracer = await TraceLens.create(config=cfg)
    try:
        yield
    finally:
        await app.state.tracer.stop()   # drains the queue, closes the DB
```

Then pass `config={"callbacks": [app.state.tracer.handler]}` on each `ainvoke`, or call
`app.state.tracer.install()` once after create to capture globally. (The `async with
TraceLens.session(...)` context manager is for scripts/one-shot runs — it tears down at
block exit.)

Then view the data out-of-band with `tracelens serve --data-dir <dir>` (or point it at
durable storage). Note the embedded SQLite + local blobs are best for single-process /
single-node today; centralized multi-process collection is on the roadmap (see
[`production_roadmap.md`](https://github.com/kjgpta/tracelens/blob/main/production_roadmap.md)).

## Sampling guidance

| Volume | Recommended sample_rate |
|---|---|
| < 100 runs/day | 1.0 (capture everything) |
| 100–10,000 runs/day | 0.5 to 1.0 |
| 10,000–100,000 runs/day | 0.1 to 0.5 |
| > 100,000 runs/day | 0.01 to 0.1, plus consider a centralized collector (roadmap) |

Sampling is per-root-run, so partial traces never happen.

## Authentication

Set `auth_token` (any random string ≥ 32 chars) and the server enforces:

- HTTP: `Authorization: Bearer <token>` on every endpoint except `/api/health`.
- WebSocket: `?token=<value>` query param OR `Sec-WebSocket-Protocol` subprotocol.

Constant-time comparison via `hmac.compare_digest` prevents timing leaks.

```bash
export TRACELENS_HOST=0.0.0.0
export TRACELENS_AUTH_TOKEN=$(openssl rand -hex 32)
```

`tracelens` will refuse to start if `host` is non-loopback and `auth_token` is unset.

## TLS

`tracelens` does not terminate TLS. Run behind a reverse proxy (nginx, Caddy,
Traefik, AWS ALB). Example nginx:

```nginx
location /tracelens/ {
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
tracelens gc --max-runs 50000
```

This deletes the oldest runs and their blobs, in FIFO order.

## Backup

The data directory is fully portable:

```bash
rsync -a --delete ~/.tracelens/ backup-host:~/tracelens_$(date +%F)/
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

- **"Refusing to start: 0.0.0.0 without TRACELENS_AUTH_TOKEN"** — set the env var.
- **"Queue drain timed out"** at shutdown — your worker fell behind; some events lost.
  Raise `queue_maxsize` or shutdown with more time.
- **"FOREIGN KEY constraint failed"** in worker logs — usually means your LangGraph
  emitted events for a run_id that wasn't preceded by `chain_start`. Worker auto-creates
  the runs row but logs the issue.
- **Events visible but no agent_name** — your custom Runnable doesn't supply a name.
  Pass `with_config(run_name="MyAgent")` on the Runnable.

## Upgrade path

A future release will add a remote-server mode (`producer → HTTP → trace server`) that
preserves your existing API. The data directory format will be compatible —
old data accessible via `tracelens serve` against the existing dir.
