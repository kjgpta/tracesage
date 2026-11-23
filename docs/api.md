# API reference

tracesage exposes a REST API under `/api` and two WebSocket endpoints under
`/ws`. This is the same surface the bundled UI consumes; you can use it
directly for programmatic queries, exports, and live tailing.

Base URL defaults to `http://127.0.0.1:7842` (or the actual bound port under
auto-port — see `tracer.ui_url`).

**Interactive docs.** FastAPI's Swagger UI is at `/docs` and the raw schema at
`/openapi.json` (ReDoc is at `/redoc`). When `auth_token` is set, these require
the same bearer token as the API. They are not part of the public UI shell.

---

## Authentication

When `auth_token` (env: `TRACESAGE_AUTH_TOKEN`) is configured, every path
requires a bearer token except `GET /api/health` and the static shell
(`GET /`, `/ui`, `/ui/*`). `/docs`, `/redoc`, `/openapi.json` and the OTLP
ingest endpoint `POST /v1/traces` are gated. `GET /` redirects to `/ui/`.

```
Authorization: Bearer <token>
```

Token comparison is constant-time. Requests without a valid token receive
`401 Unauthorized`. When no token is configured (the loopback default), auth
is a no-op.

Binding to a non-loopback host (anything other than `127.0.0.1` / `localhost`
/ `::1`) without a token is a hard fail-stop at startup.

WebSocket clients cannot set custom headers in the browser, so the token is
supplied one of these ways (checked in order):

1. `Sec-WebSocket-Protocol: tracesage.v1, tracesage.bearer.<base64url(token)>`
   (recommended; used by the bundled UI). The server selects `tracesage.v1`
   and never echoes the token.
2. `?token=<value>` query parameter (legacy; the token ends up in proxy logs
   and browser history)
3. A raw token as the `Sec-WebSocket-Protocol` value (legacy; last value wins)

A failed WebSocket handshake is closed with code `4401`. Browsers don't apply
CORS to WebSocket handshakes, so a handshake whose `Origin` is neither the
server's own host nor listed in `cors_origins` is closed with code `4403`.

`GET /api/runs/{run_id}/journey` and the WebSocket catch-up return at most the
10,000 most recent steps (`?limit=` lowers this) and set `truncated: true` when
older steps were left out. Use `/api/runs/{run_id}/export` for the full trace.

---

## REST endpoints

### `GET /api/health`

Liveness probe. Public — never requires auth.

```json
{ "status": "ok", "version": "1.0.0", "project_name": "my-app" }
```

`project_name` is the optional `TRACESAGE_PROJECT_NAME` label (`null` when unset); the
UI reads it here to show the app's name in its header.

`version` echoes the installed tracesage package version, so it always matches
whatever you have running (the example just shows the shape).

### `GET /api/runs`

List root runs, newest first.

| Query param | Type | Default | Notes |
|---|---|---|---|
| `status` | `running` \| `completed` \| `failed` \| `interrupted` \| `all` | (all) | Filter by run status. |
| `limit` | int, 1–200 | 50 | Page size. |
| `offset` | int, ≥ 0 | 0 | Pagination offset. |
| `tag` | string, 1–200 chars | (none) | Only runs whose tags contain this substring. Filtered in SQL, so it composes with `limit`/`offset` and the reported `total`. |
| `thread_id` | string, 1–200 chars | (none) | Only runs from this conversation thread (exact match). |
| `q` | string | (none) | Full-text search across step names, summaries, errors and indexed payload text. |
| `model` | string | (none) | Only runs that called this model. |
| `tool` | string | (none) | Only runs that called this tool. |
| `errors_only` | bool | false | Only failed runs. |
| `sort` | `recent` \| `oldest` \| `steps` \| `tokens` \| `cost` | `recent` | Order by start time (newest or oldest first), step count, total tokens, or total cost. |
| `no_source` | bool | false | Only runs without a `source:<app>` tag, i.e. your own agents rather than AI-app sessions. To get one app's runs, use `tag=source:<app>`. |

```json
{
  "runs": [ /* Run objects */ ],
  "total": 123,
  "limit": 50,
  "offset": 0
}
```

### `GET /api/runs/{run_id}`

Fetch a single run. `404` if the run does not exist. Returns a `Run` object.

### `GET /api/runs/{run_id}/journey`

Ordered list of steps (events) for a run. `404` if the run does not exist.

```json
{
  "run_id": "...",
  "steps": [ /* StoredEvent objects */ ],
  "truncated": false
}
```

`truncated` is `true` when older steps were dropped to stay under the limit
(see above).

### `GET /api/runs/{run_id}/steps/{event_id}/full`

Fetch the full (gzip-decoded) payload for a single blob-eligible step. The
event must belong to `run_id` (matched against `run_id` or `root_run_id`).

Returns `404` if the step is unknown, does not belong to the run, has no blob,
or the blob is missing / fails the path-traversal guard.

```json
{
  "event_id": "...",
  "run_id": "...",
  "event_type": "llm_end",
  "full_payload": { /* decoded blob */ }
}
```

### `GET /api/stats`

Aggregate stats: DB-derived counts merged with runtime counters (e.g.
`events_dropped`). `blob_size_bytes` is filled by scanning the blob dir if the
runtime value is zero. Returns a flat JSON object.

### `GET /api/topology`

The topology graph (nodes + edges). Returns a `Topology` object. Each tool node
carries a `source` field: the MCP server name it came from, or `null` for
local/hardcoded tools (see [MCP support](mcp.md)).

| Query param | Type | Default | Notes |
|---|---|---|---|
| `scope` | `run:<id>` \| `thread:<id>` \| `last_n:<N>` \| `all` | all-time | Which runs to aggregate. `run:<id>` = a single run's structure (the UI default — no stale nodes from older runs); `thread:<id>` = every run of one conversation (the UI uses it in Threads mode); `last_n:<N>` = the N most-recent runs; `all` = every run ever. A removed component (tool, agent, MCP server) only lingers under `all`. |

Scoping also drops a removed MCP server's *registered-but-uncalled* tools: a
server's tools are only shown if it was active within the scoped run(s).

### `GET /api/tools`

Tools grouped by source — each MCP server plus a `local` bucket for hardcoded
tools. Powers the UI's Tools tab. Takes the same `scope` query param
as `/api/topology` (so the panel matches the graph).

```json
{
  "sources": [
    {
      "source": "weather",
      "kind": "mcp",
      "tool_count": 3,
      "invocation_count": 12,
      "error_count": 0,
      "tools": [ { "name": "get_weather", "invocations": 5, "errors": 0 } ]
    },
    { "source": "local", "kind": "local", "tool_count": 2, "...": "..." }
  ]
}
```

MCP servers are listed first (alphabetical); the `local` bucket is last.

### `GET /api/search`

Full-text search. `q` is required (each word is quoted before it reaches FTS).
Optional `run_id` limits the search to one trace. Returns `{"hits": [...]}`.
Each hit has `event_id`, `run_id`, `snippet` and `rank`.

### `GET /api/runs/{run_id}/spans`

Latency rows for the waterfall. Each row has `run_id`, `parent_run_id`, `name`,
`kind`, `depth`, `start_ms`, `end_ms`, `duration_ms`, `status`, token counts,
`cost_usd`, `model` and `mcp_server`.

### `GET /api/runs/{run_id}/notes` and `POST /api/runs/{run_id}/notes`

List or add a note. The body is `{"text": "...", "author": "optional"}`.

### `PATCH /api/runs/{run_id}/tags`

Body `{"add": ["bug"], "remove": ["wip"]}`. Returns the updated run.

### `GET /api/threads` and `GET /api/threads/{thread_id}`

Thread list, and one thread's runs plus the conversation turns built from the
root-run payloads.

`GET /api/threads` takes `limit` (1–200, default 50), `offset`, and:

| Query param | Type | Default | Notes |
|---|---|---|---|
| `q` | string | (none) | Threads whose id contains `q`, or with a run whose step text matches it (full-text). |
| `tag` | string | (none) | Threads with a run whose tags contain this substring, e.g. `source:cursor`. |
| `sort` | `recent` \| `oldest` \| `turns` \| `tokens` | `recent` | Order by latest activity, oldest activity, turn count, or total tokens. |
| `min_turns` | int, 1–10000 | 1 | Only threads with at least this many turns. |
| `no_source` | bool | false | Only threads from your own agents (no `source:<app>` tag). |

`GET /api/threads/{thread_id}` takes `run=<run_id>` to return just that turn,
or `last=N` (1–500) to return the latest N turns. `404` if the thread is unknown.

### `GET /api/runs/{run_id}/export`

Stream a run as newline-delimited JSON (NDJSON). The first line is the `Run`
object; each subsequent line is one `StoredEvent`. Events are streamed lazily,
so the full journey is never materialized in memory.

| Query param | Type | Default | Notes |
|---|---|---|---|
| `format` | `jsonl` | `jsonl` | Only `jsonl` is supported. |

- Content-Type: `application/x-ndjson`
- Content-Disposition: `attachment; filename="<run_id>.jsonl"`
- `404` if the run does not exist.

### `DELETE /api/runs/{run_id}`

Delete a run and its blobs. Blob-deletion failures are logged but do not fail
the request. `404` if the run does not exist.

```json
{ "deleted": true, "run_id": "..." }
```

---

## Hook and log ingest

`POST /v1/hooks/{app}` accepts one JSON hook payload from Claude Code, Codex or
Cursor (`app` is `claude-code`, `codex` or `cursor`). It always responds `200 {}`
and never returns a decision, so it cannot block the agent. Bad or oversized
bodies are dropped. The same bearer token as `/v1/traces` applies.

`POST /v1/logs` accepts an OTLP log export (Codex `--otel` posts here) and
responds `200 {}`. The body is not stored.

## OTLP ingest

### `POST /v1/traces`

Accepts OpenTelemetry spans over [OTLP/HTTP](https://opentelemetry.io/docs/specs/otlp/#otlphttp)
and feeds them into the normal tracesage pipeline (sampling, per-run cap, redaction,
storage, live WebSocket, optional OTel re-export is skipped). This is the endpoint
`OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:7842` resolves to. Available on the
embedded server when `ingest=True` (default) and on `tracesage serve --ingest`; a
server without ingest answers `404`. Gated by `auth_token` like `/api/*` —
`OTEL_EXPORTER_OTLP_HEADERS="Authorization=Bearer%20<token>"`.

See [Frameworks](frameworks.md) for how spans are mapped to runs and steps.

Request:

- `Content-Type: application/x-protobuf` (OTLP default; also assumed when the header is
  missing) — `ExportTraceServiceRequest`. Needs the `tracesage[otel]` extra on the
  server; without it the response is `501`.
- `Content-Type: application/json` — OTLP/JSON (camelCase keys, hex ids, typed
  `AnyValue`s). Works with no optional dependencies. snake_case keys and base64 ids are
  also accepted.
- `Content-Encoding: gzip` or `deflate` (optional). Bodies are capped at
  `ingest_max_body_bytes` (default 16 MiB) before *and* after decompression.

Responses:

| Status | Body | When |
|---|---|---|
| `200` | protobuf `ExportTraceServiceResponse` or JSON `{}` | all spans accepted |
| `200` | `partialSuccess: { rejectedSpans, errorMessage }` | some spans could not be mapped; the rest were accepted |
| `400` | `{ "detail": "..." }` | malformed protobuf / JSON / gzip |
| `401` | `{ "detail": "..." }` | `auth_token` set and missing/invalid bearer |
| `404` | `{ "detail": "..." }` | ingest not enabled on this server |
| `413` | `{ "detail": "..." }` | body (raw or decompressed) exceeds `ingest_max_body_bytes` |
| `415` | `{ "detail": "..." }` | unsupported `Content-Type` or `Content-Encoding` |
| `501` | `{ "detail": "..." }` | protobuf body but `opentelemetry-proto` is not installed |

Event ids derive from span ids, so re-sending a batch (exporter retry) is idempotent.

```bash
curl -X POST http://127.0.0.1:7842/v1/traces \
  -H 'content-type: application/json' \
  --data '{"resourceSpans":[{"resource":{"attributes":[{"key":"service.name","value":{"stringValue":"demo"}}]},
    "scopeSpans":[{"spans":[{"traceId":"0af7651916cd43dd8448eb211c80319c","spanId":"b7ad6b7169203331",
    "name":"chat gpt-4o","kind":3,"startTimeUnixNano":"1700000000000000000","endTimeUnixNano":"1700000000650000000",
    "attributes":[{"key":"gen_ai.operation.name","value":{"stringValue":"chat"}},
                  {"key":"gen_ai.request.model","value":{"stringValue":"gpt-4o"}},
                  {"key":"gen_ai.usage.input_tokens","value":{"intValue":"12"}}],
    "status":{"code":1}}]}]}]}'
# {}
```

---

## WebSocket endpoints

Both endpoints push `WSMessage` frames (JSON) and tolerate slow/dead clients
(per-socket send timeout; dead sockets are dropped without blocking others).

### `WS /ws/trace/{run_id}`

Per-run live feed. On connect, the server sends one `catchup` message
containing every step recorded so far, then tails new steps as they arrive.
The client may send any text frames (e.g. keepalive pings); they are drained
and ignored. The connection stays open until the client disconnects.

### `WS /ws/runs`

Global feed across all runs — used by the UI run list to live-update as new
runs start and complete.

---

## Object shapes

These are the Pydantic models in `src/tracesage/models.py` (the authoritative
schema — endpoints serialize them directly). Timestamps are ISO-8601 UTC strings.

### `Run`

| Field | Type | Notes |
|---|---|---|
| `run_id` | string | The root run's ID (UUIDv7, time-ordered). |
| `root_run_id` | string | Same as `run_id` for a root run. |
| `tags` | string[] | Tags propagated from `config={"tags": [...]}`. |
| `status` | `running` \| `completed` \| `failed` \| `interrupted` | Run lifecycle state. `interrupted` means a LangGraph `interrupt()` paused the run; resuming starts a new run on the same `thread_id`. |
| `thread_id` | string \| null | Conversation thread, from the LangGraph `thread_id` (or `session_id` / `conversation_id`) in callback metadata. |
| `started_at` | datetime | When the root run began. |
| `completed_at` | datetime \| null | Null while still running. |
| `total_steps` | int | Event count for the run. |
| `total_tokens_input` / `total_tokens_output` | int | Summed token usage the models reported (0 if none). |
| `graph_definition` | string \| null | Serialized graph structure, when available. |
| `error_message` | string \| null | Set when `status = failed`. |

### `StoredEvent`

A single step in a run's journey (`/journey`, `/export`, WS `catchup`).

| Field | Type | Notes |
|---|---|---|
| `event_id` | string | Unique per event; used in the `/full` path. |
| `run_id` / `parent_run_id` / `root_run_id` | string / string\|null / string | Nesting links. |
| `event_type` | `EventType` | e.g. `chain_start`, `llm_end`, `tool_end`, `*_error` (see `EventType` enum). |
| `timestamp` | datetime | Event time. |
| `agent_name` / `tool_name` | string \| null | Populated by kind. |
| `mcp_server` | string \| null | Provenance — the MCP server a tool came from; `null` = local/hardcoded. |
| `summary` | string | Truncated to `summary_max_chars`. |
| `blob_path` | string \| null | Relative path to the gzipped full payload (fetch via `/full`); `null` if not blob-eligible. |
| `duration_ms` | int \| null | For `*_end` events. |
| `token_input` / `token_output` | int \| null | Per-event token usage (LLM events). |
| `error_message` | string \| null | For `*_error` events. |

### `Stats`

Returned (merged with DB-derived counts) by `GET /api/stats`.

| Field | Type | Notes |
|---|---|---|
| `queue_depth` / `queue_max` | int | Current vs max ingestion queue size. |
| `events_processed` | int | Total events written to the DB. |
| `events_dropped` | int | Events lost to a full queue (backpressure) — should stay 0. |
| `events_sampled_out` | int | Events skipped by `sample_rate`. |
| `runs_throttled` | int | Runs that hit `per_run_event_cap`. |
| `last_write_latency_ms` / `p99_write_latency_ms` | float \| null | Most-recent and rolling-p99 batch-write latency. |
| `db_size_bytes` / `blob_size_bytes` | int | On-disk sizes. |

### `Topology`

`{ "nodes": TopologyNode[], "edges": TopologyEdge[] }`.

**`TopologyNode`:** `id` (`"<kind>:<name>"`, e.g. `tool:search_web`), `name`,
`type` (`agent`\|`tool`\|`llm`\|`retriever`\|`chain`\|`mcp`), `source` (MCP server
for tool/mcp nodes; `null` = local), `invocation_count`, `error_count`,
`total_duration_ms`, `avg_duration_ms`, `last_seen`.

**`TopologyEdge`:** `source`, `target` (node IDs), `count`, `last_seen`.

### `WSMessage`

WebSocket frame: `msg_type` (`event`\|`run_update`\|`catchup`\|`topology_update`\|`error`),
`run_id`, and a `payload` object whose shape depends on `msg_type` (a `StoredEvent`
for `event`, a `Run` for `run_update`, a list of `StoredEvent`s for `catchup`).
