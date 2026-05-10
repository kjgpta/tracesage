# Configuration

All settings live on `TraceLensConfig` (Pydantic Settings). Override via:

1. Constructor kwargs: `TraceLensConfig(port=8000)`
2. Environment variables prefixed `TRACELENS_`: `TRACELENS_PORT=8000`

## Settings reference

| Setting | Env var | Default | Notes |
|---|---|---|---|
| `host` | `TRACELENS_HOST` | `127.0.0.1` | Bind address. Refuses non-loopback without `auth_token`. |
| `port` | `TRACELENS_PORT` | `7842` | `0` = ephemeral; bound port readable via `tracer.bound_port`. |
| `auth_token` | `TRACELENS_AUTH_TOKEN` | `None` | Bearer token for HTTP + WebSocket auth. |
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
| `max_blob_size_gb` | `TRACELENS_MAX_BLOB_SIZE_GB` | `10.0` | Soft cap; warned in stats. |
| `log_level` | `TRACELENS_LOG_LEVEL` | `WARNING` | Python logging level. |

## Production safety rails

- `host=0.0.0.0` (or any non-loopback) **without** `auth_token` is a hard fail-stop:
  `TraceLensConfig` raises `ValueError` at construction.
- `sample_rate` outside `[0, 1]` raises.
- All numeric caps must be positive.

## Sampling behavior

Sampling is decided **once per root run**: the first event from a new root
flips a coin against `sample_rate`. If accepted, every descendant event of
that root is captured. If rejected, every descendant is dropped. This avoids
half-traced runs.

## Per-run event cap (circuit breaker)

When a single run exceeds `per_run_event_cap` events (default 50K), the
tracer marks it `throttled` and silently drops further events for that run.
Other runs are unaffected. Visible via `stats.runs_throttled` and the run's
`status` (still `running`, but tag added).

## Blob retention

Blobs grow with `chain_end` / `llm_end` / `tool_end` / `agent_finish` /
`retriever_end` events. Periodically run `tracelens gc --max-runs 5000` to
trim oldest runs and their blobs.

## Cross-machine deployment

The `data_dir` is portable — `rsync` or copy it to another host, then run
`tracelens serve --data-dir <path>` to view it remotely. The `traces.db`
file (plus `-wal` / `-shm` siblings) and `blobs/` directory are all you need.
