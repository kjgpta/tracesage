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

Use cases:

- Inspect traces after the producer process exited.
- Run the viewer on a different machine than the producer (sync `data_dir` first).
- Restart only the UI without restarting the application.

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
- `--format, -f FORMAT` — `jsonl` only in v0.1

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
tracelens stats [--data-dir PATH]
```

Output:
- `total_runs` — count of root runs
- `running` / `completed` / `failed` — counts by status
- `avg_duration_ms` — average across completed runs
- `total_tokens_input` / `total_tokens_output` — sums
- `db_size_bytes` — SQLite file size

## `tracelens gc`

Enforce retention. Deletes oldest runs (and their blobs) beyond the cap.

```bash
tracelens gc [OPTIONS]
```

Options:

- `--data-dir, -d PATH`
- `--max-runs N` — keep at most N most-recent root runs (default 10000)
- `--dry-run` — print what would be deleted; no changes

Schedule via cron / systemd timer:

```bash
# /etc/cron.daily/tracelens-gc
tracelens gc --data-dir /var/lib/tracelens --max-runs 50000
```

## `tracelens version`

Print version.

```bash
tracelens version
# tracelens 0.1.0
```
