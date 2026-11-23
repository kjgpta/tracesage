# Privacy and data

tracesage runs entirely on your machine. The collector listens on `127.0.0.1`
only, reads the AI apps' local files read-only, and writes to one folder. Nothing
is uploaded, and there is no telemetry.

## What is stored

By default a recorded turn keeps what the app saved: your prompt, the assistant's
reply and thinking, each tool call's input and output, model names, token counts,
cost and timing. That is the point of a trace, and it also means the data dir
holds the same sensitive text as your chats.

## Where it lives

Everything is under the data dir, `~/.tracesage` by default:

| Path | What it holds |
|---|---|
| `traces.db` | Runs, steps, tags, notes and the search index (SQLite) |
| `blobs/` | Full step payloads, gzipped |
| `session-imports.json` | Which Claude Code, Cursor, Codex and Cowork turns were already imported |
| `desktop-chat-imports.json` | The same for Claude Desktop sidebar chats |
| `connect.json` | The collector's port and the `--metadata-only` choice |
| `spool/<app>.jsonl` | Hook events written while the collector was down; replayed on start |
| `bin/forward` | The small script Cursor and Codex hooks run |
| `logs/collector.log` | The collector's output |

`connect` also leaves `*.tracesage.bak` copies of each app config it changed, next
to the original.

## Keep less

**Metadata only.** Connect with `--metadata-only` to keep names, timing, tokens
and status, but no prompt, reply or tool text:

```bash
tracesage connect --metadata-only
```

This sets `TRACESAGE_HOOK_METADATA_ONLY=true` in the collector service, and it
applies to the session importers as well as the hooks. Running `connect` again
without the flag turns it off. Turns already stored are not changed.

**Redaction.** `TRACESAGE_REDACT_PATTERNS` (a JSON list of regexes) scrubs
matches from summaries and payloads before they are stored. To use it with the
collector, add it to the service's environment: the `EnvironmentVariables` dict
in `~/Library/LaunchAgents/dev.tracesage.collector.plist` on macOS, or the
`[Service]` section of the systemd unit on Linux. Then restart the collector. See
[Redaction](../configuration.md#redaction).

**Search index.** `TRACESAGE_SEARCH_INDEX_PAYLOADS=false` keeps payload text out
of the full-text index (names, summaries and errors are still searchable).

## Delete runs

- One run: the **Delete** button on its row in the UI, `DELETE /api/runs/{run_id}`,
  or an assistant's `delete_run` (the MCP server must be started with
  `--allow-delete`).
- Old runs: `tracesage gc` keeps the newest `TRACESAGE_MAX_RUNS` (10,000 by
  default) and can cap the blob size; see [CLI: gc](../cli.md#tracesage-gc).

The importer remembers what it imported, so a deleted turn is not imported again
unless its content changes in the app, or you reset the import state (below).

## Import again from scratch

To rebuild everything from the apps' files (for example after changing
`--metadata-only`), stop the collector, remove the import state, and start it:

```bash
tracesage service stop
rm ~/.tracesage/session-imports.json ~/.tracesage/desktop-chat-imports.json
tracesage service start
```

Step ids are derived from the app's own ids, so turns that are still stored are
updated in place rather than duplicated. Cursor and Codex go back
`TRACESAGE_IMPORT_DAYS` (30) days.

## Uninstall

```bash
tracesage disconnect          # restore every app config from its backup
tracesage service uninstall   # stop the collector and remove the login service
rm -rf ~/.tracesage           # delete all recorded data
```

Then `pip uninstall tracesage` or `uv tool uninstall tracesage`. If you added the
Claude Desktop extension (`.mcpb`), remove it under **Settings → Extensions**.
