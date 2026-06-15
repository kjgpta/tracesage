# CLI reference

The `tracelens` command-line tool ships with the package. It is a **viewer +
utilities** — it does NOT ingest events. Ingestion only happens when your
Python code calls `TraceLens.create()`.

## `tracelens serve`

Start a read-only viewer over an existing data directory.

```bash
tracelens serve [OPTIONS]
```

Options:

- `--data-dir, -d PATH` — path to existing data dir (default: `~/.tracelens`)
- `--host, -h HOST` — bind address (default `127.0.0.1`)
- `--port, -p PORT` — bind port (default `7842`)
- `--auth-token TOKEN` — bearer token (env: `TRACELENS_AUTH_TOKEN`)
- `--open, -o` — open the viewer in your browser once it's up

Use cases:

- Inspect traces after the producer process exited.
- Run the viewer on a different machine than the producer (sync `data_dir` first).
- Restart only the UI without restarting the application.

## `tracelens demo`

Seed a sample trace and open the UI — the fastest way to see tracelens working.

```bash
tracelens demo               # seed + serve + open browser
tracelens demo --check       # seed only, then exit (for smoke tests / CI)
```

## `tracelens show`

Render a run's trace as an indented tree in the terminal (no server needed).

```bash
tracelens show <run_id> [--no-color]
```

## `tracelens watch`

Live-tail a run's events in the terminal as they're written (Ctrl-C to stop).

```bash
tracelens watch <run_id> [--interval 1.0] [--once]
```

`--once` prints the current events and exits (no follow loop).

## `tracelens diff`

Compare two runs side by side (status, steps, tokens, tools, errors).

```bash
tracelens diff <run_a> <run_b>
```

## `tracelens view`

Open an exported JSONL trace directly in the UI (imports into a temp dir, serves it).

```bash
tracelens view trace.jsonl [--open]
```

## `tracelens export`

Dump runs to JSONL.

```bash
tracelens export [OPTIONS]
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
tracelens export --run-id order-8821 -o trace.jsonl
tracelens export --all -o all_traces.jsonl
```

## `tracelens stats`

Print summary stats.

```bash
tracelens stats [--data-dir PATH] [--json]
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

## `tracelens runs`

List root runs.

```bash
tracelens runs [OPTIONS]
```

Options:

- `--data-dir, -d PATH`
- `--status STATUS` — `all` | `running` | `completed` | `failed` (default `all`)
- `--limit, -l N` — max rows (default 50)
- `--offset N` — pagination offset
- `--tag TEXT` — only runs whose tags contain this substring (client-side filter)
- `--json` — emit one JSON object per line (NDJSON)

```bash
tracelens runs --status failed --limit 20
tracelens runs --json | jq -r .run_id | head
```

## `tracelens gc`

Enforce retention. Deletes oldest runs (and their blobs) beyond the cap.

```bash
tracelens gc [OPTIONS]
```

Options:

- `--data-dir, -d PATH`
- `--max-runs N` — keep at most N most-recent root runs (default 10000)
- `--max-blob-size-gb GB` — after the run-count pass, also delete oldest runs
  until total blob size is under this many GB
- `--dry-run` — print what would be deleted; no changes

Schedule via cron / systemd timer:

```bash
# /etc/cron.daily/tracelens-gc
tracelens gc --data-dir /var/lib/tracelens --max-runs 50000 --max-blob-size-gb 5
```

## `tracelens import`

Inverse of `export`: read a JSONL export back into a data directory (creating it
if needed). Useful for backups and moving runs between machines.

```bash
tracelens import [OPTIONS]
```

Options:

- `--data-dir, -d PATH` — target data dir
- `--input, -i PATH` — JSONL file, or `-` for stdin (default `-`)

```bash
tracelens export --all -o backup.jsonl
tracelens import --data-dir ~/.tracelens-restore -i backup.jsonl
```

One malformed line is skipped with a warning rather than aborting the import.

## `tracelens doctor`

Read-only diagnostics for a data directory: schema version, run counts, and
orphan/missing-blob checks.

```bash
tracelens doctor [--data-dir PATH]
```

## `tracelens version`

Print version.

```bash
tracelens version
# tracelens 0.2.0
```
