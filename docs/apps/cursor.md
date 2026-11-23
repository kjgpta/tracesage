# Cursor

Record Cursor agent chats as traces: each model step, each tool call (edits,
terminal commands, searches, MCP calls) with its input, output and timing, and
each subagent nested under the call that started it.

## Connect it

```bash
pip install "tracesage[apps]"        # or: uv tool install "tracesage[apps]"
tracesage connect cursor
```

Restart Cursor if it was open, then open
[http://127.0.0.1:7842](http://127.0.0.1:7842) and set **Source** to Cursor.
Chats from the last 30 days are imported on first start.

## What is recorded, and from where

Cursor keeps every chat in its state database, which the collector opens
read-only:

| Platform | File |
|---|---|
| macOS | `~/Library/Application Support/Cursor/User/globalStorage/state.vscdb` |
| Windows | `%APPDATA%\Cursor\User\globalStorage\state.vscdb` |
| Linux | `~/.config/Cursor/User/globalStorage/state.vscdb` |

| What | Notes |
|---|---|
| Chats and turns | A chat becomes a thread, each message you send a run. |
| Model steps | Cursor does not store one row per model request, so a step is inferred from each thinking or text segment, or a gap between tool calls. |
| Tool calls | Name, parameters, result, status and timing. MCP calls are grouped under their server. |
| Subagents | Each has its own Cursor chat; it is nested under the tool call that spawned it. |

## What `connect` changes

- `~/.cursor/hooks.json`: command hooks (`sessionStart`, `beforeSubmitPrompt`,
  `postToolUse`, `afterMCPExecution`, `afterAgentResponse`, `stop` and others)
  that run `~/.tracesage/bin/forward cursor`. The forwarder posts to the
  collector, or appends to `~/.tracesage/spool/cursor.jsonl` when it is down.
- The collector service, if it is not installed yet.

While the collector can read `state.vscdb` it ignores these hooks, so turns are
not recorded twice.

## Options

| Flag | Effect |
|---|---|
| `--metadata-only` | Keep names, timing and status, but not prompt, reply or tool text. |
| `--mcp` | Add tracesage to `~/.cursor/mcp.json`, so Cursor's agent can query traces. See [MCP server](../mcp.md). |
| `--no-trace` | Skip the hooks; useful with `--mcp` alone. |
| `--dry-run`, `--print cursor` | Show the planned changes and write nothing. |

Set `TRACESAGE_IMPORT_DAYS` before the first start to import more than 30 days
(see [Configuration](../configuration.md#ai-app-collector)).

## Limits

- Tokens appear only when Cursor recorded them for a step.
- Cost is usually `n/a`: Cursor's model names (such as `default`) do not map to
  a price.
- Chats older than the import window are skipped on first start.

## Disconnect

```bash
tracesage disconnect cursor
```

This restores `hooks.json` (and `mcp.json` if you used `--mcp`) from the
backups. If `hooks.json` did not exist before, remove the tracesage entries by
hand. Recorded runs stay until you delete them; see
[Privacy and data](privacy-and-data.md).
