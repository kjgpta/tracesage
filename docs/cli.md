# CLI reference

The `tracesage` command-line tool ships with the package. It is a **viewer +
utilities** — it does NOT ingest events. Ingestion only happens when your
Python code calls `TraceSage.create()`.

## `tracesage serve`

Start a read-only viewer over an existing data directory.

```bash
tracesage serve [OPTIONS]
```

Options:

- `--data-dir, -d PATH` — path to existing data dir (default: `~/.tracesage`)
- `--host, -h HOST` — bind address (default `127.0.0.1`)
- `--port, -p PORT` — bind port (default `7842`)
- `--auth-token TOKEN` — bearer token (env: `TRACESAGE_AUTH_TOKEN`)
- `--open, -o` — open the viewer in your browser once it's up

Use cases:

- Inspect traces after the producer process exited.
- Run the viewer on a different machine than the producer (sync `data_dir` first).
- Restart only the UI without restarting the application.

## `tracesage demo`

Seed a sample trace and open the UI — the fastest way to see tracesage working.

```bash
tracesage demo               # seed + serve + open browser
tracesage demo --check       # seed only, then exit (for smoke tests / CI)
```

## `tracesage show`

Render a run's trace as an indented tree in the terminal (no server needed).

```bash
tracesage show <run_id> [--no-color]
```

## `tracesage watch`

Live-tail a run's events in the terminal as they're written (Ctrl-C to stop).

```bash
tracesage watch <run_id> [--interval 1.0] [--once]
```

`--once` prints the current events and exits (no follow loop).

## `tracesage diff`

Compare two runs side by side (status, steps, tokens, tools, errors).

```bash
tracesage diff <run_a> <run_b>
```

## `tracesage view`

Open an exported JSONL trace directly in the UI (imports into a temp dir, serves it).

```bash
tracesage view trace.jsonl [--open]
```

## `tracesage export`

Dump runs to JSONL.

```bash
tracesage export [OPTIONS]
```

Options:

- `--data-dir, -d PATH`
- `--run-id ID` — single run to export
- `--all` — export every run
- `--output, -o PATH` — file path or `-` for stdout (default: `-`)
- `--format, -f FORMAT` — `jsonl` only (for now)

Output format: first line per run is the `Run` row; subsequent lines are
`StoredEvent` rows. Each line is JSON with a `_kind` discriminator (`run` or
`event`).

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
- `running` / `completed` / `failed` — counts by status
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
- `--status STATUS` — `all` | `running` | `completed` | `failed` (default `all`)
- `--limit, -l N` — max rows (default 50)
- `--offset N` — pagination offset
- `--tag TEXT` — only runs whose tags contain this substring (client-side filter)
- `--json` — emit one JSON object per line (NDJSON)

```bash
tracesage runs --status failed --limit 20
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
if needed). Useful for backups and moving runs between machines.

```bash
tracesage import [OPTIONS]
```

Options:

- `--data-dir, -d PATH` — target data dir
- `--input, -i PATH` — JSONL file, or `-` for stdin (default `-`)

```bash
tracesage export --all -o backup.jsonl
tracesage import --data-dir ~/.tracesage-restore -i backup.jsonl
```

One malformed line is skipped with a warning rather than aborting the import.

## `tracesage doctor`

Read-only diagnostics for a data directory: schema version, run counts, and
orphan/missing-blob checks.

```bash
tracesage doctor [--data-dir PATH]
```

## `tracesage version`

Print version.

```bash
tracesage version
# tracesage 0.2.0
```
