# API reference

tracesage exposes a REST API under `/api` and two WebSocket endpoints under
`/ws`. This is the same surface the bundled UI consumes; you can use it
directly for programmatic queries, exports, and live tailing.

Base URL defaults to `http://127.0.0.1:7842`.

---

## Authentication

When `auth_token` (env: `TRACESAGE_AUTH_TOKEN`) is configured, every endpoint
requires a bearer token **except** `GET /api/health` and the static `/ui`
shell:

```
Authorization: Bearer <token>
```

Token comparison is constant-time. Requests without a valid token receive
`401 Unauthorized`. When no token is configured (the loopback default), auth
is a no-op.

Binding to a non-loopback host (anything other than `127.0.0.1` / `localhost`
/ `::1`) without a token is a hard fail-stop at startup.

WebSocket clients cannot set custom headers in the browser, so the token is
supplied one of two ways (checked in order):

1. `?token=<value>` query parameter
2. `Sec-WebSocket-Protocol` subprotocol header (last value wins if multiple)

A failed WebSocket handshake is closed with code `4401`.

---

## REST endpoints

### `GET /api/health`

Liveness probe. Public — never requires auth.

```json
{ "status": "ok", "version": "0.2.1", "project_name": "my-app" }
```

`project_name` is the optional `TRACESAGE_PROJECT_NAME` label (`null` when unset); the
UI reads it here to show the app's name in its header.

`version` echoes the installed tracesage package version, so it always matches
whatever you have running (the example just shows the shape).

### `GET /api/runs`

List root runs, newest first.

| Query param | Type | Default | Notes |
|---|---|---|---|
| `status` | `running` \| `completed` \| `failed` \| `all` | (all) | Filter by run status. |
| `limit` | int, 1–200 | 50 | Page size. |
| `offset` | int, ≥ 0 | 0 | Pagination offset. |
| `tag` | string, 1–200 chars | (none) | Only runs whose tags contain this substring. Filtered in SQL, so it composes with `limit`/`offset` and the reported `total`. |

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
  "steps": [ /* StoredEvent objects */ ]
}
```

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
| `scope` | `run:<id>` \| `last_n:<N>` \| `all` | all-time | Which runs to aggregate. `run:<id>` = a single run's structure (the UI default — no stale nodes from older runs); `last_n:<N>` = the N most-recent runs; `all` = every run ever. A removed component (tool, agent, MCP server) only lingers under `all`. |

Scoping also drops a removed MCP server's *registered-but-uncalled* tools: a
server's tools are only shown if it was active within the scoped run(s).

### `GET /api/tools`

Tools grouped by source — each MCP server plus a `local` bucket for hardcoded
tools. Powers the UI's "Tools by source" panel. Takes the same `scope` query param
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

The `Run`, `StoredEvent`, `Stats`, and `Topology` shapes are defined as
Pydantic models in `src/tracesage/models.py`. Export output and the journey /
full-step responses serialize those models directly, so the model definitions
are the authoritative schema.
