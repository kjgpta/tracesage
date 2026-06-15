# Production deployment guide

This page covers what to know before running tracelens against real traffic.

## Authentication

When binding to anything other than loopback, an auth token is **required**:

```bash
TRACELENS_HOST=0.0.0.0 \
TRACELENS_AUTH_TOKEN=<32-byte-random> \
python app.py
```

Without `auth_token`, the tracer **raises at construction**. This is a hard
fail-stop, not a warning — the rule prevents accidental exposure of trace
data (which contains prompts, tool args, and outputs) on a public network.

Generate a token:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

How auth is enforced:

- **HTTP**: `Authorization: Bearer <token>` on every request except `/api/health`
- **WebSocket**: token in `?token=...` query param OR via `Sec-WebSocket-Protocol`
- Comparison is constant-time (`hmac.compare_digest`)

## Sampling

For high-volume systems, capture a fraction of root runs:

```bash
TRACELENS_SAMPLE_RATE=0.1 python app.py    # 10% of runs
```

The sampling decision is at the **root run level** — once a root passes, all
its descendant events are captured (you never get a partial trace). If a root
is sampled out, all its descendants are dropped.

This is critical: per-event sampling would give you incoherent traces with
holes in the middle of agent runs. Root-level sampling preserves the property
that any captured run is complete.

## Per-run event cap

A single buggy agent in an infinite loop could fill the queue. The per-run
cap is a circuit breaker:

```bash
TRACELENS_PER_RUN_EVENT_CAP=50000 python app.py    # default
```

Once a single root run exceeds the cap, additional events from that root are
dropped (and counted in `runs_throttled`). Other runs continue normally.

## Retention

There is no automatic eviction; old runs accumulate. Run `tracelens gc` on a
schedule:

```bash
# Crontab: keep last 30k runs, run nightly at 02:00
0 2 * * * tracelens gc --max-runs 30000 --data-dir /var/tracelens
```

For multi-tenant deployments, you may want per-tag retention. There is no
built-in per-tag GC; query and delete via the REST API or `tracer.db`:

```python
runs, _ = await tracer.db.list_runs(limit=1_000_000)
for r in runs:
    if "tenant:departed" in r.tags:
        await tracer.db.delete_run(r.run_id)
        await tracer.blob_store.delete_run(r.run_id)
```

## Storage planning

Per-event size:

- DB row: ~1 KB (summary + metadata)
- Optional gzipped blob (LLM_END, CHAIN_END, AGENT_FINISH, TOOL_END,
  RETRIEVER_END): typically 1-50 KB after gzip

For 1M events with 20% blob-eligible:

- DB: ~1 GB
- Blobs: ~5-10 GB

Windows NTFS is significantly slower than Linux ext4/xfs for many small
gzipped writes. If you're on Windows and seeing queue depth grow, raise
`TRACELENS_WORKER_BATCH_SIZE` to 200 and `TRACELENS_WORKER_BATCH_TIMEOUT` to
0.5s — fewer, larger batches amortize the fsync cost.

## Health monitoring

`/api/health` is unauthenticated, returns `{"status": "ok", "version": "..."}`.

`/api/stats` (authenticated) returns the in-process stats:

```json
{
  "queue_depth": 12,
  "queue_max": 50000,
  "events_dropped": 0,
  "events_processed": 14782,
  "events_sampled_out": 0,
  "runs_throttled": 0,
  "last_write_latency_ms": 23.4,
  "p99_write_latency_ms": 89.2,
  "db_size_bytes": 1048576,
  "blob_size_bytes": 9437184
}
```

Things to alert on:

- `events_dropped > 0` → queue overflow; raise `queue_maxsize` or sample more
- `p99_write_latency_ms > 5000` → disk struggling; investigate IO
- `runs_throttled > 0` → per-run event cap firing; check for runaway agents
- `queue_depth` growing over time → worker can't keep up with producer

## Multi-tenant deployments

Two patterns:

### Pattern A — separate data dirs, separate viewers

```bash
TRACELENS_DATA_DIR=/var/trace/tenant-a python app.py
TRACELENS_DATA_DIR=/var/trace/tenant-b python app.py
tracelens serve --data-dir /var/trace/tenant-a --port 7842
tracelens serve --data-dir /var/trace/tenant-b --port 7843
```

Pros: hard isolation, independent retention, separate auth tokens.
Cons: N viewers for N tenants.

### Pattern B — one data dir, tags for tenancy

```python
config={
    "callbacks": [tracer.handler],
    "tags": [f"tenant:{tenant_id}"],
}
```

Pros: one viewer URL. Cons: no isolation if a tenant fills the per-run cap;
one tenant's data is visible to anyone with viewer access.

Pick A when tenants must not see each other's traces. Pick B when the data
isn't sensitive across tenants and operational simplicity matters more.

## Embedded vs sidecar

The default is embedded: one process produces traces and serves the UI.
Acceptable for development and small production. For larger production:

```python
# producer (no UI exposed externally)
tracer = await TraceLens.create(TraceLensConfig(host="127.0.0.1"))
```

```bash
# viewer (separate process, exposed with auth)
tracelens serve \
    --data-dir /shared/data \
    --host 0.0.0.0 \
    --port 7842 \
    --auth-token "$TRACELENS_AUTH_TOKEN"
```

The data dir must be on shared storage (NFS, EBS, etc.) reachable by both
processes. SQLite handles concurrent readers fine; only one writer at a time.

## Pre-flight checklist

Before pointing real traffic at tracelens:

- [ ] `auth_token` set if binding non-loopback
- [ ] `data_dir` on a disk with enough space + IO headroom
- [ ] `sample_rate` set if event rate > ~100/s
- [ ] `tracelens gc` scheduled
- [ ] `/api/stats` scraped by your monitoring stack
- [ ] Alerts on `events_dropped`, `p99_write_latency_ms`, `runs_throttled`
- [ ] Tags planned: system, version, environment, tenant
- [ ] Backup strategy for `data_dir` if loss isn't acceptable

## Upgrades

The on-disk schema is versioned via `PRAGMA user_version`. On connect, tracelens:

- Runs automatic schema migrations (additive only) — your data dir survives upgrades
- Leaves existing rows intact (new columns/tables are added, never dropped)

You don't need to do anything on upgrade except deploy the new package and
restart your process. The data dir survives across upgrades.

## What tracelens doesn't ship yet

Be aware of these limits before standardizing on tracelens for critical
production:

- **Single-writer per data dir.** Multiple producer processes against the
  same data dir is not supported yet (on the roadmap — see `production_roadmap.md`).
- **No OpenTelemetry export yet.** Planned for v0.3+. If you need OTel today,
  consider Phoenix or LangFuse alongside tracelens.
- **No built-in eval framework.** tracelens is for tracing; pair it with a
  separate evals tool (deepeval, ragas, etc.).
- **No managed cloud.** Self-hosted only; that's intentional.
