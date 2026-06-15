# Configuration

All settings live on `TraceLensConfig` (Pydantic Settings). Override via:

1. Constructor kwargs: `TraceLensConfig(port=8000)`
2. Environment variables prefixed `TRACELENS_`: `TRACELENS_PORT=8000`

## Settings reference

| Setting | Env var | Default | Notes |
|---|---|---|---|
| `enabled` | `TRACELENS_ENABLED` | `True` | **Kill switch.** `False` returns an inert tracer — no server, no DB/worker, a no-op handler, near-zero overhead. See [Disabling](#disabling-kill-switch). |
| `host` | `TRACELENS_HOST` | `127.0.0.1` | Bind address. Refuses non-loopback without `auth_token`. |
| `port` | `TRACELENS_PORT` | `7842` | `0` = ephemeral; bound port readable via `tracer.bound_port`. |
| `auth_token` | `TRACELENS_AUTH_TOKEN` | `None` | Bearer token for HTTP + WebSocket auth. |
| `public_url` | `TRACELENS_PUBLIC_URL` | `None` | Base URL used for run deep-links (e.g. behind a reverse proxy). Falls back to the bound host/port. |
| `print_run_url` | `TRACELENS_PRINT_RUN_URL` | `True` | Print a `🔍 tracelens: <url>` link to stderr on each new root run. Set `False` in noisy/prod environments. |
| `start_server` | `TRACELENS_START_SERVER` | `True` | Start the embedded UI server in-process. Set `False` in prod to keep capturing without a web server (view later with `tracelens serve`). A `start_server=` kwarg to `create()`/`session()` overrides this. |
| `cors_origins` | `TRACELENS_CORS_ORIGINS` | `["*"]` | Allowed CORS origins. Tighten to an explicit allowlist when exposing beyond localhost. |
| `startup_health_timeout_s` | `TRACELENS_STARTUP_HEALTH_TIMEOUT_S` | `3.0` | How long `create()` waits for the embedded server to report started. |
| `data_dir` | `TRACELENS_DATA_DIR` | `~/.tracelens` | Root data directory (DB + blobs). |
| `db_filename` | `TRACELENS_DB_FILENAME` | `traces.db` | SQLite filename inside `data_dir`. |
| `blob_subdir` | `TRACELENS_BLOB_SUBDIR` | `blobs` | Blob subdirectory inside `data_dir`. |
| `db_pool_size` | `TRACELENS_DB_POOL_SIZE` | `5` | Max concurrent DB connections. |
| `queue_maxsize` | `TRACELENS_QUEUE_MAXSIZE` | `50000` | Drop new events if exceeded; counter exposed in `/api/stats`. |
| `worker_batch_size` | `TRACELENS_WORKER_BATCH_SIZE` | `50` | Max events per DB transaction. |
| `worker_batch_timeout` | `TRACELENS_WORKER_BATCH_TIMEOUT` | `0.1` | Seconds; how long the worker waits for a full batch before flushing. |
| `sample_rate` | `TRACELENS_SAMPLE_RATE` | `1.0` | `0.0–1.0`; per-run sampling decided once per root. |
| `per_run_event_cap` | `TRACELENS_PER_RUN_EVENT_CAP` | `50000` | Circuit breaker per run. |
| `summary_max_chars` | `TRACELENS_SUMMARY_MAX_CHARS` | `500` | Max summary length stored in `events.summary`. |
| `max_runs` | `TRACELENS_MAX_RUNS` | `10000` | Retention cap; enforce via `tracelens gc`. |
| `max_blob_size_gb` | `TRACELENS_MAX_BLOB_SIZE_GB` | `10.0` | Retention target; enforced by `tracelens gc --max-blob-size-gb`. |
| `redact_patterns` | `TRACELENS_REDACT_PATTERNS` | `[]` | Opt-in: regex patterns scrubbed from summaries + payloads before storage. Empty = off. |
| `redact_replacement` | `TRACELENS_REDACT_REPLACEMENT` | `[REDACTED]` | String that matched redaction patterns are replaced with. |
| `log_level` | `TRACELENS_LOG_LEVEL` | `WARNING` | Python logging level. |

## Disabling (kill switch)

Set `enabled=False` (or `TRACELENS_ENABLED=false`) to make tracelens a complete
no-op: `TraceLens.create()` / `tracelens.trace()` / `TraceLens.session()` return an
inert tracer with no embedded server, no DB/worker/queue, and a no-op callback handler.
This lets you wire tracelens in once and switch it off per-environment (e.g. in prod)
without touching your integration code. The bind-safety fail-stop below is skipped when
disabled (nothing binds). See [production.md](production.md) for the measured overhead
when disabled.

```bash
export TRACELENS_ENABLED=false      # complete no-op
```

## Production safety rails

- `host=0.0.0.0` (or any non-loopback) **without** `auth_token` is a hard fail-stop:
  `TraceLensConfig` raises `ValueError` at construction (unless `enabled=False`).
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
TraceLensConfig(redact_patterns=[r"sk-[A-Za-z0-9]{20,}", r"\b\d{16}\b"])
```

## Blob retention

Blobs grow with `chain_end` / `llm_end` / `tool_end` / `agent_finish` /
`retriever_end` events. Periodically run `tracelens gc` to trim oldest runs and
their blobs — by count (`--max-runs 5000`) and/or by total size
(`--max-blob-size-gb 5`).

## Cross-machine deployment

The `data_dir` is portable — `rsync` or copy it to another host, then run
`tracelens serve --data-dir <path>` to view it remotely. The `traces.db`
file (plus `-wal` / `-shm` siblings) and `blobs/` directory are all you need.
