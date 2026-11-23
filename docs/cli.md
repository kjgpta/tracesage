# CLI reference

The `tracesage` command-line tool ships with the package. It is a **viewer +
utilities**. By default it does NOT ingest events — ingestion happens when your
Python code calls `TraceSage.create()` — with one exception: `tracesage serve --ingest`
runs a standalone OpenTelemetry collector.

## `tracesage serve`

Start a viewer over a data directory (read-only unless `--ingest`).

```bash
tracesage serve [OPTIONS]
```

Options:

- `--data-dir, -d PATH` — path to existing data dir (default: `~/.tracesage`)
- `--host, -h HOST` — bind address (default `127.0.0.1`)
- `--port, -p PORT` — bind port (default `7842`). If it's busy, the next free port is
  bound automatically (the real URL is printed on startup). Set `TRACESAGE_PORT_AUTO=false`
  to use exactly this port instead, or `--port 0` for an OS-assigned ephemeral port.
- `--auth-token TOKEN` — bearer token (env: `TRACESAGE_AUTH_TOKEN`)
- `--open, -o` — open the viewer in your browser once it's up
- `--ingest` — also accept OpenTelemetry spans on `POST /v1/traces` (OTLP/HTTP,
  protobuf or JSON, gzip ok) and store them in this data dir

So you can run a viewer for several data dirs at once — each lands on `7842`, `7843`, …
without clashing.

Use cases:

- Inspect traces after the producer process exited.
- Run the viewer on a different machine than the producer (sync `data_dir` first).
- Restart only the UI without restarting the application.
- With `--ingest`: collect traces from Pydantic AI, the OpenAI Agents SDK, CrewAI, the
  OpenAI / Anthropic clients, or any other OTel-instrumented process (any language):

```bash
pip install "tracesage[otel]"                 # protobuf decoding (JSON works without it)
tracesage serve --ingest -d ~/.tracesage --open
# tracesage collector at http://127.0.0.1:7842/ui  (data: ~/.tracesage)
#   OTLP/HTTP ingest: POST http://127.0.0.1:7842/v1/traces

# in the producer:
export OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:7842
export OTEL_SERVICE_NAME=my-agent
```

Ingested spans run through the full tracesage pipeline — sampling, per-run cap,
redaction, worker — and appear live in the UI. All `TRACESAGE_*` settings apply
(`TRACESAGE_SAMPLE_RATE`, `TRACESAGE_REDACT_PATTERNS`, `TRACESAGE_INGEST_MAX_BODY_BYTES`, …).
See [Frameworks](frameworks.md) and [API → `POST /v1/traces`](api.md#post-v1traces).

## `tracesage demo`

Seed a sample trace and open the UI — the fastest way to see tracesage working.

```bash
tracesage demo               # seed + serve + open browser
tracesage demo --check       # seed only, then exit (for smoke tests / CI)
```

Note: `demo` writes to its own dir (`~/.tracesage-demo` by default, override with
`-d`), separate from the default `~/.tracesage`, so it never mixes with your real
runs.

## `tracesage show`

Render a run's trace as an indented tree in the terminal (no server needed). Each
element kind (chain, agent, tool, llm, retriever, …) is printed in its own colour,
tools backed by an MCP server are tagged `mcp:<server>`, and siblings follow
execution flow (oldest-first).

```bash
tracesage show <run_id> [-d DIR] [--no-color] [--reverse] [--waterfall]
```

- `--data-dir, -d PATH` — data dir (default `~/.tracesage`). Required when the
  writer used a different `data_dir` than the default.
- `--no-color` — disable ANSI colour (auto-detected by default)
- `--reverse, -r` — order siblings newest-first instead of execution order
- `--waterfall` — print a latency bar per span instead of the tree

## `tracesage watch`

Live-tail a run's events in the terminal as they're written (Ctrl-C to stop).

```bash
tracesage watch <run_id> [-d DIR] [--interval 1.0] [--once]
```

- `--data-dir, -d PATH` — data dir (default `~/.tracesage`)
- `--interval SECONDS` — poll interval (default `1.0`)
- `--once` — print the current events and exit (no follow loop)

## `tracesage diff`

Compare two runs side by side (status, steps, tokens, tools, errors).

```bash
tracesage diff <run_a> <run_b> [-d DIR]
```

- `--data-dir, -d PATH` — data dir both runs are read from (default `~/.tracesage`)

## `tracesage view`

Open an exported JSONL trace directly in the UI (imports into a temp dir, serves it).

```bash
tracesage view trace.jsonl [--open]
```

## `tracesage export`

Dump runs to JSONL.

```bash
tracesage export [RUN_ID] [OPTIONS]
```

Options:

- `RUN_ID` (positional) — single run to export; alternative to `--run-id`
- `--data-dir, -d PATH`
- `--run-id ID` — single run to export
- `--all` — export every run
- `--output, -o PATH` — file path or `-` for stdout (default: `-`)
- `--format, -f FORMAT` — `jsonl` only (for now)

Output format: first line per run is the `Run` row; subsequent lines are
`StoredEvent` rows. Each line is JSON with a `_kind` discriminator (`run` or
`event`).

Only those structured fields are written. The gzipped full-payload blobs
(`blobs/{run_id}/{event_id}.json.gz`) are **not** included, so a later
`import` restores the run and its steps but not the original request/response
bodies. To move those too, copy the data directory itself.

```bash
tracesage export --run-id order-8821 -o trace.jsonl
tracesage export --all -o all_traces.jsonl
```

## `tracesage stats`

Print summary stats.

```bash
tracesage stats [--data-dir PATH] [--json]
```

Options:

- `--data-dir, -d PATH`
- `--json` — emit a single JSON object instead of human-readable key/value lines

Output:
- `total_runs` — count of root runs
- `running` / `completed` / `failed` / `interrupted` — counts by status
- `avg_duration_ms` — average across completed runs
- `total_tokens_input` / `total_tokens_output` — sums
- `db_size_bytes` — SQLite file size

## `tracesage runs`

List root runs.

```bash
tracesage runs [OPTIONS]
```

Options:

- `--data-dir, -d PATH`
- `--status STATUS` — `all` | `running` | `completed` | `failed` | `interrupted` (default `all`)
- `--limit, -l N` — max rows (default 50)
- `--offset N` — pagination offset
- `--tag TEXT` — only runs whose tags contain this substring (filtered in SQL, so
  it composes correctly with `--limit`/`--offset` and the reported total)
- `--thread ID` — only runs from this conversation thread (exact `thread_id` match)
- `--json` — emit one JSON object per line (NDJSON)

```bash
tracesage runs --status failed --limit 20
tracesage runs --thread user-42   # every turn of one conversation, newest first
tracesage runs --json | jq -r .run_id | head
```

## `tracesage gc`

Enforce retention. Deletes oldest runs (and their blobs) beyond the cap.

```bash
tracesage gc [OPTIONS]
```

Options:

- `--data-dir, -d PATH`
- `--max-runs N` — keep at most N most-recent root runs (default 10000)
- `--max-blob-size-gb GB` — after the run-count pass, also delete oldest runs
  until total blob size is under this many GB
- `--dry-run` — print what would be deleted; no changes

Schedule via cron / systemd timer:

```bash
# /etc/cron.daily/tracesage-gc
tracesage gc --data-dir /var/lib/tracesage --max-runs 50000 --max-blob-size-gb 5
```

## `tracesage import`

Inverse of `export`: read a JSONL export back into a data directory (creating it
if needed). Useful for moving the structured trace between machines. Full
payload blobs are not in the file, so imported steps have no blob to open in
the drawer. Copy `data_dir` when you need those.

```bash
tracesage import [INPUT] [OPTIONS]
```

Options:

- `INPUT` (positional) — JSONL file; alternative to `--input`
- `--data-dir, -d PATH` — target data dir
- `--input, -i PATH` — JSONL file, or `-` for stdin (default `-`)

```bash
tracesage export --all -o backup.jsonl
tracesage import --data-dir ~/.tracesage-restore -i backup.jsonl
tracesage import backup.jsonl                       # positional form
```

One malformed line is skipped with a warning rather than aborting the import.

## `tracesage doctor`

Read-only diagnostics for a data directory: schema version, run counts, and
orphan/missing-blob checks.

```bash
tracesage doctor [--data-dir PATH]
```

## `tracesage search`

```bash
tracesage search "seaside hotel" [-d DIR] [--limit 20]
```

Print matching steps from the FTS index. Each line is a run id, an event id and
a snippet.

## `tracesage thread`

```bash
tracesage thread <thread_id> [-d DIR]
```

Print the conversation turns for one thread, including cost when it is known.

## `tracesage reindex`

```bash
tracesage reindex [-d DIR]
```

Walk stored events, fill payload text into the search index, and price `llm_end`
rows that have no cost yet. `tracesage doctor` says when a database still needs
this (it does, once, after the schema v5 upgrade).

## `tracesage connect` / `disconnect`

```bash
pip install "tracesage[apps]"
tracesage connect [app|all] [--metadata-only] [--mcp] [--no-trace] [--no-service] [--otel] [--dry-run] [--yes] [--print APP] [--mcpb]
tracesage connect --status
tracesage disconnect [app|all]
```

`connect` records the sessions of installed AI apps in the local collector. It
shows each file it will change, asks once (`--yes` skips the question), keeps a
`*.tracesage.bak` backup of each file, installs and starts the collector service,
and the collector imports past sessions on its first start. See
[AI apps](ai-apps.md).

| `app` | What `connect` writes | How turns are recorded |
|---|---|---|
| `claude-code` | HTTP hooks in `~/.claude/settings.json` | Transcripts in `~/.claude/projects` |
| `codex` (alias `chatgpt`) | Command hooks in `~/.codex/hooks.json`; trust them with `/hooks` in Codex | Rollouts in `~/.codex/sessions` |
| `cursor` | Command hooks in `~/.cursor/hooks.json` | Cursor's `state.vscdb`, read-only |
| `claude-desktop` | Nothing (an MCP entry with `--mcp`) | Desktop's chat cache, IndexedDB and Cowork logs |
| `vscode`, `windsurf`, `gemini` | Nothing (an MCP entry with `--mcp`) | Not recorded |
| `all` (default) | Every installed app above | |

The hooks are a fallback: while the collector can read an app's own files, it
ignores that app's hooks so a turn is not recorded twice.

| Flag | Effect |
|---|---|
| `--metadata-only` | Keep names, timing, tokens and status; drop prompt, reply and tool text. Sets `TRACESAGE_HOOK_METADATA_ONLY` on the service. |
| `--mcp` | Also add tracesage as an MCP server (`~/.claude.json`, `~/.codex/config.toml`, `~/.cursor/mcp.json`, `claude_desktop_config.json`, VS Code `mcp.json`, ...). |
| `--mcpb` | Also build `tracesage.mcpb` in the data dir, a Claude Desktop extension (needs `npx`). Without `npx`, download it from the [latest release](https://github.com/kjgpta/tracesage/releases/latest/download/tracesage.mcpb). |
| `--otel` | Also point Claude Code's or Codex's own OpenTelemetry export at the collector. |
| `--no-trace` | Skip the session hooks (use with `--mcp`). |
| `--no-service` | Write the configs but do not install or start the collector. |
| `--dry-run` | Print the planned changes and write nothing. |
| `--print APP` | Print the planned config for one app and write nothing. |
| `--yes`, `-y` | Do not ask before writing. |
| `--status` | Show the collector port, the latest run per source, and which apps are connected. |
| `-d DIR` | Data dir (default `~/.tracesage`). |

```text
$ tracesage connect --status
collector port: 7842 (listening)
recent sources:
  source:claude-code: last run 2026-11-22T10:12:40+00:00
installed: claude-code, cursor, claude-desktop
  claude-code hooks: connected
  codex: not configured
  cursor hooks: connected
  cursor mcp: present, no tracesage entry
  claude desktop: present, no tracesage entry
```

`disconnect [app|all]` copies each `*.tracesage.bak` back over the file it came
from. A file that `connect` created has no backup; remove its tracesage entry by
hand. Per-app guides: [Claude Desktop](apps/claude-desktop.md),
[Claude Code](apps/claude-code.md), [Cursor](apps/cursor.md),
[Codex](apps/codex.md).

## `tracesage service`

```bash
tracesage service install|uninstall|start|stop|status [-d DIR]
```

`install` writes a LaunchAgent, a systemd user unit, or (on start) a Windows
logon task that runs `tracesage serve --ingest` on `127.0.0.1`. `start` loads
it (`launchctl bootstrap`, `systemctl --user enable --now`, or `schtasks`).
Logs go to `~/.tracesage/logs/collector.log`. The port is in
`~/.tracesage/connect.json` when 7842 was taken.

## `tracesage mcp`

```bash
tracesage mcp [-d DIR] [--read-only] [--allow-delete]
```

stdio MCP server for a coding agent. Requires `tracesage[apps]`. `--read-only`
disables `add_note`, `set_tags` and `delete_run`. `delete_run` also needs
`--allow-delete` and `confirm: true` from the caller. Writes are logged to stderr.
`tracesage connect <app> --mcp` registers it with an app; see
[Claude Desktop: ask about your traces](apps/claude-desktop.md#ask-claude-about-your-traces)
for the tool list.

## `tracesage version`

Print version.

```bash
tracesage version
# tracesage 1.0.0   (prints your installed version)
```
